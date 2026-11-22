"""AI knowledge base: approved answers per customer topic (plan B2).

Each topic (``intent_key``) has short articles in versions. Only an APPROVED
article reaches the AI, as part of the cached prompt prefix (prompt_text). A
draft comes from staff or from the offline drafting tool (scripts/kb_pipeline.py)
and is approved by an Admin or Super Admin. Approved articles are never edited in
place: "Revise" opens a new draft version, and approving it retires the previous
version. "Observed practice" (what agents actually did) is stored for reviewers
and is never shown to the AI. Every change is audited in ai_kb_audit.
"""
from __future__ import annotations

import json
import re
from typing import Any

import aiomysql

from app.config import settings
from app.support_auth import SupportAgent

STATUSES = ("DRAFT", "APPROVED", "RETIRED")
EXAMPLE_STYLES = ("en", "fr", "ar", "leb_ar", "leb_arabizi", "mixed")
_INTENT = re.compile(r"^[a-z0-9_]{2,64}$")
# De-identification placeholders left in a draft must be replaced before approval.
_PLACEHOLDER = re.compile(r"<(?:SECRET|EMAIL|URL|AMOUNT|NAME|PHONE|ID)(?:_\d+)?>")
# Above this the knowledge base no longer fits comfortably in the cached prefix
# (plan B2: about 10-15k tokens); retrieval by topic is then needed.
PREFIX_TOKEN_BUDGET = 15_000


class AIKnowledgeError(Exception):
    pass


class AIKnowledgeForbiddenError(AIKnowledgeError):
    pass


class AIKnowledgeValidationError(AIKnowledgeError):
    pass


class AIKnowledgeConflictError(AIKnowledgeError):
    pass


class AIKnowledgeNotFoundError(AIKnowledgeError):
    pass


def _text(payload: dict[str, Any], key: str, *, limit: int, required: bool = False) -> str | None:
    value = payload.get(key)
    value = str(value).strip() if value is not None else ""
    if required and not value:
        raise AIKnowledgeValidationError(f"{key.replace('_', ' ').capitalize()} is required.")
    if len(value) > limit:
        raise AIKnowledgeValidationError(f"{key.replace('_', ' ').capitalize()} is too long (at most {limit} characters).")
    return value or None


def _examples(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 6:
        raise AIKnowledgeValidationError("Give at most six example replies.")
    examples = []
    for item in value:
        if not isinstance(item, dict):
            raise AIKnowledgeValidationError("Each example needs a style, a customer message and a reply.")
        style = str(item.get("style") or "").strip()
        customer = str(item.get("customer") or "").strip()
        reply = str(item.get("reply") or "").strip()
        if style not in EXAMPLE_STYLES or not customer or not reply or len(customer) > 500 or len(reply) > 1000:
            raise AIKnowledgeValidationError(
                "Each example needs a style (" + ", ".join(EXAMPLE_STYLES) + "), a customer message and a reply."
            )
        examples.append({"style": style, "customer": customer, "reply": reply})
    return examples


def validate_article(payload: dict[str, Any]) -> dict[str, Any]:
    intent = str(payload.get("intent_key") or "").strip().lower()
    if not _INTENT.match(intent):
        raise AIKnowledgeValidationError("Topic keys use 2-64 lowercase letters, digits or underscores.")
    title = _text(payload, "title", limit=191, required=True)
    if len(title or "") < 3:
        raise AIKnowledgeValidationError("Give the topic a title of at least 3 characters.")
    return {
        "intent_key": intent,
        "title": title,
        "approved_policy": _text(payload, "approved_policy", limit=6000, required=True),
        "data_needed": _text(payload, "data_needed", limit=500),
        "escalate_when": _text(payload, "escalate_when", limit=3000),
        "examples": _examples(payload.get("examples")),
        "observed_practice": _text(payload, "observed_practice", limit=6000),
    }


def estimate_tokens(text: str) -> int:
    # About four characters per token for English; Arabic script is denser, so
    # this errs high, which is the safe side for a budget.
    return (len(text) + 3) // 4


def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    article = dict(row)
    article["examples"] = json.loads(article.pop("examples_json") or "[]")
    article["source_ticket_ids"] = json.loads(article.get("source_ticket_ids") or "[]")
    for key in ("created_at", "updated_at", "approved_at", "retired_at"):
        if article.get(key) is not None and hasattr(article[key], "isoformat"):
            article[key] = article[key].isoformat()
    return article


class AIKnowledgeRepository:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=settings.db_host, port=settings.db_port, user=settings.db_user,
            password=settings.db_password, db=settings.db_name, minsize=1, maxsize=3,
            autocommit=True, charset="utf8mb4",
        )

    async def _fetch(self, query: str, args: tuple = (), *, one: bool = False):
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, args)
                return await (cur.fetchone() if one else cur.fetchall())

    async def list(self, status: str | None = None) -> list[dict[str, Any]]:
        where, args = ("WHERE status = %s", (status,)) if status else ("", ())
        return list(await self._fetch(
            f"SELECT * FROM ai_kb_articles {where} ORDER BY intent_key, version DESC", args,
        ))

    async def get(self, article_id: int) -> dict[str, Any] | None:
        return await self._fetch("SELECT * FROM ai_kb_articles WHERE id = %s", (article_id,), one=True)

    async def audit_for(self, article_id: int) -> list[dict[str, Any]]:
        return list(await self._fetch(
            "SELECT action, actor_staff_name, details_json, created_at FROM ai_kb_audit WHERE article_id = %s ORDER BY id",
            (article_id,),
        ))

    @staticmethod
    async def _audit(cur: Any, article_id: int, intent: str, action: str, actor: SupportAgent | None,
                     details: dict[str, Any] | None = None) -> None:
        await cur.execute(
            """
            INSERT INTO ai_kb_audit (article_id, intent_key, action, actor_staff_id, actor_staff_name,
                                     details_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
            """,
            (article_id, intent, action, int(actor.id) if actor else None, actor.name if actor else None,
             json.dumps(details or {}, ensure_ascii=False)),
        )

    async def _transaction(self):
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        return self._pool.acquire()

    async def create_draft(self, values: dict[str, Any], *, actor: SupportAgent | None, source: str,
                           source_ticket_count: int | None = None,
                           source_ticket_ids: list[int] | None = None) -> int:
        async with await self._transaction() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT id, version, status FROM ai_kb_articles WHERE intent_key = %s ORDER BY version DESC FOR UPDATE",
                        (values["intent_key"],),
                    )
                    rows = list(await cur.fetchall())
                    if any(row["status"] == "DRAFT" for row in rows):
                        raise AIKnowledgeConflictError("This topic already has a draft. Edit that draft instead.")
                    version = (int(rows[0]["version"]) + 1) if rows else 1
                    await cur.execute(
                        """
                        INSERT INTO ai_kb_articles (
                            intent_key, version, status, title, approved_policy, data_needed, escalate_when,
                            examples_json, observed_practice, source, source_ticket_count, source_ticket_ids,
                            owner_staff_id, created_by_staff_id, updated_by_staff_id, created_at, updated_at
                        ) VALUES (%s, %s, 'DRAFT', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                  UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                        """,
                        (values["intent_key"], version, values["title"], values["approved_policy"],
                         values["data_needed"], values["escalate_when"],
                         json.dumps(values["examples"], ensure_ascii=False), values["observed_practice"], source,
                         source_ticket_count, json.dumps(source_ticket_ids or []),
                         int(actor.id) if actor else None, int(actor.id) if actor else None,
                         int(actor.id) if actor else None),
                    )
                    article_id = int(cur.lastrowid)
                    await self._audit(cur, article_id, values["intent_key"], "DRAFT_CREATED", actor,
                                      {"version": version, "source": source})
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return article_id

    async def _locked(self, cur: Any, article_id: int, row_version: int | None) -> dict[str, Any]:
        await cur.execute("SELECT * FROM ai_kb_articles WHERE id = %s FOR UPDATE", (article_id,))
        row = await cur.fetchone()
        if not row:
            raise AIKnowledgeNotFoundError("Article not found.")
        if row_version is not None and int(row["row_version"]) != int(row_version):
            raise AIKnowledgeConflictError("Someone else changed this article. Reload it before saving.")
        return row

    async def update_draft(self, article_id: int, values: dict[str, Any], *, row_version: int,
                           actor: SupportAgent) -> None:
        async with await self._transaction() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    row = await self._locked(cur, article_id, row_version)
                    if row["status"] != "DRAFT":
                        raise AIKnowledgeConflictError("Only drafts can be edited. Use Revise to change an approved article.")
                    if values["intent_key"] != row["intent_key"]:
                        raise AIKnowledgeValidationError("The topic key of an article cannot change.")
                    await cur.execute(
                        """
                        UPDATE ai_kb_articles
                        SET title = %s, approved_policy = %s, data_needed = %s, escalate_when = %s,
                            examples_json = %s, observed_practice = %s, updated_by_staff_id = %s,
                            row_version = row_version + 1, updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (values["title"], values["approved_policy"], values["data_needed"], values["escalate_when"],
                         json.dumps(values["examples"], ensure_ascii=False), values["observed_practice"],
                         int(actor.id), article_id),
                    )
                    await self._audit(cur, article_id, row["intent_key"], "DRAFT_EDITED", actor)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def approve(self, article_id: int, *, row_version: int, actor: SupportAgent) -> None:
        async with await self._transaction() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    row = await self._locked(cur, article_id, row_version)
                    if row["status"] != "DRAFT":
                        raise AIKnowledgeConflictError("Only a draft can be approved.")
                    text = " ".join(str(row.get(key) or "") for key in ("approved_policy", "escalate_when", "examples_json"))
                    if _PLACEHOLDER.search(text):
                        raise AIKnowledgeValidationError(
                            "Replace the placeholders such as <PHONE_1> or <AMOUNT_1> with real policy text before approving."
                        )
                    await cur.execute(
                        "SELECT id FROM ai_kb_articles WHERE intent_key = %s AND status = 'APPROVED' FOR UPDATE",
                        (row["intent_key"],),
                    )
                    for previous in await cur.fetchall():
                        await cur.execute(
                            """
                            UPDATE ai_kb_articles SET status = 'RETIRED', retired_at = UTC_TIMESTAMP(6),
                                row_version = row_version + 1, updated_at = UTC_TIMESTAMP(6)
                            WHERE id = %s
                            """,
                            (previous["id"],),
                        )
                        await self._audit(cur, int(previous["id"]), row["intent_key"], "RETIRED", actor,
                                          {"replaced_by": article_id})
                    await cur.execute(
                        """
                        UPDATE ai_kb_articles
                        SET status = 'APPROVED', approved_by_staff_id = %s, approved_by_staff_name = %s,
                            approved_at = UTC_TIMESTAMP(6), row_version = row_version + 1, updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (int(actor.id), actor.name, article_id),
                    )
                    await self._audit(cur, article_id, row["intent_key"], "APPROVED", actor, {"version": row["version"]})
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def retire(self, article_id: int, *, row_version: int, actor: SupportAgent) -> None:
        async with await self._transaction() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    row = await self._locked(cur, article_id, row_version)
                    if row["status"] == "RETIRED":
                        raise AIKnowledgeConflictError("This article is already retired.")
                    await cur.execute(
                        """
                        UPDATE ai_kb_articles SET status = 'RETIRED', retired_at = UTC_TIMESTAMP(6),
                            row_version = row_version + 1, updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (article_id,),
                    )
                    await self._audit(cur, article_id, row["intent_key"], "RETIRED", actor)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise


class AIKnowledgeService:
    def __init__(self, repository: AIKnowledgeRepository | None = None) -> None:
        self.repository = repository or AIKnowledgeRepository()

    @staticmethod
    def _require_manager(agent: SupportAgent) -> None:
        if not agent.is_supervisor:
            raise AIKnowledgeForbiddenError("Only Super Admins and Admins can manage the AI knowledge base.")

    async def overview(self, agent: SupportAgent, *, status: str | None = None) -> dict[str, Any]:
        self._require_manager(agent)
        if status is not None and status not in STATUSES:
            raise AIKnowledgeValidationError("Unknown article status.")
        articles = [_decode(row) for row in await self.repository.list(status)]
        approved = await self.repository.list("APPROVED")
        tokens = estimate_tokens(self._prompt_from(approved))
        return {
            "articles": articles,
            "approved_topics": len(approved),
            "prompt_tokens_estimate": tokens,
            "prompt_token_budget": PREFIX_TOKEN_BUDGET,
            "over_budget": tokens > PREFIX_TOKEN_BUDGET,
            "styles": list(EXAMPLE_STYLES),
        }

    async def article(self, agent: SupportAgent, article_id: int) -> dict[str, Any]:
        self._require_manager(agent)
        row = _decode(await self.repository.get(article_id))
        if row is None:
            raise AIKnowledgeNotFoundError("Article not found.")
        row["audit"] = [
            {**entry, "created_at": entry["created_at"].isoformat() if hasattr(entry["created_at"], "isoformat")
             else entry["created_at"]}
            for entry in await self.repository.audit_for(article_id)
        ]
        return row

    async def create(self, agent: SupportAgent, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_manager(agent)
        article_id = await self.repository.create_draft(validate_article(payload), actor=agent, source="STAFF")
        return await self.article(agent, article_id)

    async def create_tool_draft(self, payload: dict[str, Any], *, source_ticket_count: int,
                                source_ticket_ids: list[int]) -> int:
        """Store a draft written by the offline drafting tool (never approved automatically)."""
        return await self.repository.create_draft(
            validate_article(payload), actor=None, source="AI_DRAFT",
            source_ticket_count=source_ticket_count, source_ticket_ids=source_ticket_ids[:200],
        )

    async def update(self, agent: SupportAgent, article_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_manager(agent)
        await self.repository.update_draft(article_id, validate_article(payload),
                                           row_version=int(payload.get("row_version") or 0), actor=agent)
        return await self.article(agent, article_id)

    async def revise(self, agent: SupportAgent, article_id: int) -> dict[str, Any]:
        """Open a new draft version from an approved (or retired) article."""
        self._require_manager(agent)
        source = _decode(await self.repository.get(article_id))
        if source is None:
            raise AIKnowledgeNotFoundError("Article not found.")
        if source["status"] == "DRAFT":
            raise AIKnowledgeConflictError("This article is already a draft. Edit it directly.")
        new_id = await self.repository.create_draft(validate_article(source), actor=agent, source="STAFF")
        return await self.article(agent, new_id)

    async def approve(self, agent: SupportAgent, article_id: int, row_version: int) -> dict[str, Any]:
        self._require_manager(agent)
        await self.repository.approve(article_id, row_version=row_version, actor=agent)
        return await self.article(agent, article_id)

    async def retire(self, agent: SupportAgent, article_id: int, row_version: int) -> dict[str, Any]:
        self._require_manager(agent)
        await self.repository.retire(article_id, row_version=row_version, actor=agent)
        return await self.article(agent, article_id)

    async def prompt_text(self) -> str:
        """The approved knowledge base as it goes into the AI's cached prompt prefix.

        Stable ordering (by topic key) keeps the prefix byte-identical between calls,
        which is what makes prompt caching work. Observed practice and internal
        ticket references are never included.
        """
        return self._prompt_from(await self.repository.list("APPROVED"))

    @staticmethod
    def _prompt_from(rows: list[dict[str, Any]]) -> str:
        sections = []
        for row in sorted(rows, key=lambda item: str(item.get("intent_key"))):
            article = _decode(row) or {}
            lines = [f"## {article['title']} [{article['intent_key']}]", article["approved_policy"]]
            if article.get("data_needed"):
                lines.append(f"Data needed: {article['data_needed']}")
            if article.get("escalate_when"):
                lines.append(f"Hand over to staff when: {article['escalate_when']}")
            for example in article.get("examples") or []:
                lines.append(f"Example ({example['style']}): customer: {example['customer']} | reply: {example['reply']}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)


ai_knowledge_service = AIKnowledgeService()
