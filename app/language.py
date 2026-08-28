from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    language: str
    style: str


_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÿ]")

_LEBANESE_ARABIC_WORDS = {
    "وين", "شو", "بدي", "بديي", "بدّي", "تبعي", "تبعنا", "صار", "صارت", "فوت",
    "اليوزر", "يوزر", "الباسوورد", "باسوورد", "الباسورد", "باسورد",
    "هلق", "هلّق", "فيني", "فيك", "كيفك", "مرحبا", "مرحباً", "قديش", "ليش",
    "إيمتى", "امتى", "بتوصل", "الشحنة", "شحنة", "معي", "عندي", "اي", "إيه",
    "طيب", "وإذا", "واذا", "اشحن", "إشحن", "بشحن", "قديش",
}
_FORMAL_ARABIC_WORDS = {
    "أين", "متى", "شحنتي", "الشحنة", "يرجى", "أريد", "أرغب", "يمكنكم", "التسليم",
    "الوصول", "حالة", "موعد", "فضلاً", "شكراً", "اسم", "المستخدم", "كلمة", "المرور", "السر",
}
_ARABIZI_WORDS = {
    "wen", "wein", "bade", "baddi", "badde", "taba3e", "taba3i", "taba3ak",
    "sar", "saret", "marhaba", "kifak", "kifik", "iza", "fik", "fina", "shu",
    "shou", "hal2", "halla2", "emta", "imta", "btousal", "betousal", "addeh",
    "mesh", "mish", "yalla", "merci", "shipment", "wel", "w", "fout", "l", "el",
    "shefle", "tshefle", "chefle", "choufle", "shoufle", "choufe", "shoufe",
    "shouf", "shuf", "3tine", "a3tine", "b3atle", "ba3atle", "nkammel",
    "kammel", "ma3", "ma3ak", "men", "mn", "hon", "hawn", "tene", "tenye",
    "3a", "bl", "bel", "eza", "tyb", "tayeb", "tamem", "baddak", "baddik",
    "baddo", "rah", "fi", "ma", "ba3d", "aw", "la", "esh7an", "eshhan",
    "she7an", "she7ne", "sh7an", "adesh", "ade", "mawed", "mawad", "tejmil",
    "tajmil", "emarat", "imarat", "lebnen", "lebnan", "su3udiye", "so3oudiye",
    "sou3oudiye", "sa3oudiye", "s3oudiye", "saudiye", "betkallef", "btekallef",
    "bikallef", "kellef", "jaw", "barr", "ba7er", "amerka", "amerka",
    "amerika", "souriya", "suriya", "terkiya", "turkiya", "torkiya",
    "ekseswar", "exeswar", "akseswar", "3ira2", "3iraq", "ysallemon",
    "yisallmo", "yisallmak",
}

# A single strong Lebanese transliteration marker is enough to keep a short message
# in Arabizi even when it naturally embeds an English logistics noun, e.g.
# ``bade tshefle order`` or ``shefle el order``. Generic bridge words such as ``w``
# and ``l`` are deliberately excluded because they are too weak on their own.
_STRONG_ARABIZI_WORDS = {
    "wen", "wein", "bade", "baddi", "badde", "taba3e", "taba3i", "taba3ak",
    "sar", "saret", "marhaba", "kifak", "kifik", "iza", "fik", "fina", "shu",
    "shou", "hal2", "halla2", "emta", "imta", "btousal", "betousal", "addeh",
    "mesh", "mish", "yalla", "fout", "shefle", "tshefle", "chefle", "choufle",
    "shoufle", "choufe", "shoufe", "shouf", "shuf", "3tine", "a3tine",
    "b3atle", "ba3atle", "nkammel", "kammel", "ma3", "ma3ak", "hon",
    "hawn", "tene", "tenye", "eza", "tyb", "tayeb", "esh7an", "eshhan",
    "she7an", "she7ne", "sh7an", "adesh", "mawed", "mawad", "tejmil", "tajmil",
    "emarat", "imarat", "lebnen", "lebnan", "su3udiye", "so3oudiye",
    "sou3oudiye", "sa3oudiye", "s3oudiye", "saudiye", "betkallef", "btekallef",
    "bikallef", "kellef", "baddak", "baddik", "baddo", "ba3d", "amerka",
    "amerka", "amerika", "souriya", "suriya", "terkiya", "turkiya",
    "torkiya", "ekseswar", "exeswar", "akseswar", "3ira2", "3iraq",
    "ysallemon", "yisallmo", "yisallmak",
}
_FRENCH_WORDS = {
    "bonjour", "salut", "livraison", "colis", "merci", "où", "ou", "quand", "arrive",
    "arriver", "expédition", "expedition", "suivi", "pouvez", "svp", "s'il", "client",
    "identifiant", "utilisateur", "mot", "passe", "mon", "connecter", "et", "si",
    "depuis", "vers", "arabie", "saoudite", "emirats", "émirats", "liban", "combien",
    "coûte", "coute", "prix", "tarif", "cosmétiques", "cosmetiques", "avion", "mer",
    "route", "terrestre", "expédier", "expedier", "envoyer", "poids",
}
_ENGLISH_WORDS = {
    "where", "shipment", "delivery", "tracking", "when", "arrive", "package", "hello",
    "hi", "please", "thanks", "status", "check", "customer", "support",
    "user", "username", "password", "pass", "login", "account", "order", "orders",
    "what", "about", "from", "to", "saudi", "arabia", "emirates", "lebanon", "cost",
    "price", "ship", "cosmetics", "electronics", "air", "sea", "land", "thank",
    "you", "can", "much", "of", "the", "is", "are",
}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-zÀ-ÿ0-9]+|[\u0600-\u06FF]+", text.casefold()))


def detect_communication_style(
    text: str | None,
    previous_style: str | None = None,
) -> LanguageProfile:
    value = str(text or "").strip()
    if not value:
        return LanguageProfile("en", "en")

    words = _words(value)
    has_arabic = bool(_ARABIC_RE.search(value))
    has_latin = bool(_LATIN_RE.search(value))

    arabizi_score = len(words & _ARABIZI_WORDS)
    strong_arabizi_score = len(words & _STRONG_ARABIZI_WORDS)
    # Arabizi numerals are meaningful only alongside Latin letters.
    if has_latin and re.search(r"(?i)(?:[a-z][235789][a-z]|[235789][a-z]{2,}|[a-z]{2,}[235789])", value):
        arabizi_score += 2

    french_score = len(words & _FRENCH_WORDS)
    english_score = len(words & _ENGLISH_WORDS)

    if has_arabic:
        leb_score = len(words & _LEBANESE_ARABIC_WORDS)
        formal_score = len(words & _FORMAL_ARABIC_WORDS)
        if has_latin:
            # Mixed Arabic script and Latin is best treated as mixed while retaining
            # Arabic directionality in the UI via dir="auto".
            return LanguageProfile("mixed", "mixed")
        if leb_score > 0 and leb_score >= formal_score:
            return LanguageProfile("ar", "leb_ar")
        return LanguageProfile("ar", "ar")

    if strong_arabizi_score >= 1:
        # Lebanese customers frequently keep words such as order, shipment, user ID,
        # and password in English while the surrounding sentence remains Arabizi.
        # One genuine Lebanese transliteration marker therefore wins over a small
        # number of embedded English nouns. A real French signal keeps the result
        # mixed, matching the customer's actual blend.
        if french_score >= 1:
            return LanguageProfile("mixed", "mixed")
        if english_score <= 3 or arabizi_score >= 2:
            return LanguageProfile("ar-LB", "leb_arabizi")

    if arabizi_score >= 2:
        # Lebanese Arabizi naturally embeds English auth/shipping nouns such as
        # user/password/shipment; those alone should not force a mixed-language
        # classification. A French signal or heavier English phrasing does.
        if french_score >= 1 or english_score >= 3:
            return LanguageProfile("mixed", "mixed")
        return LanguageProfile("ar-LB", "leb_arabizi")

    if french_score > english_score and french_score > 0:
        detected = LanguageProfile("fr", "fr")
    elif english_score > 0:
        detected = LanguageProfile("en", "en")
    elif has_latin:
        detected = LanguageProfile("mixed", "mixed")
    else:
        detected = LanguageProfile("en", "en")

    # Short catalogue fragments often consist mostly of English product nouns even
    # though the customer is still speaking Lebanese Arabizi (for example,
    # ``electronics men amerka``). Keep the established style unless the current turn
    # contains clear evidence of a real language switch.
    if previous_style == "leb_arabizi" and has_latin:
        if french_score >= 2 and french_score > english_score:
            return LanguageProfile("fr", "fr")
        explicit_english_phrase = bool(
            re.search(
                r"(?i)\b(?:what|where|when|how|can|could|please|thank\s+you|i\s+want|"
                r"how\s+much)\b",
                value,
            )
        ) and english_score >= 2
        if not explicit_english_phrase and len(words) <= 8:
            return LanguageProfile("ar-LB", "leb_arabizi")

    if previous_style == "fr" and has_latin:
        explicit_english_phrase = bool(
            re.search(r"(?i)\b(?:what|where|when|how|can|please|thank\s+you)\b", value)
        ) and english_score >= 2
        if not explicit_english_phrase and french_score == 0 and len(words) <= 4:
            return LanguageProfile("fr", "fr")

    if previous_style == "mixed" and has_latin and len(words) <= 4:
        if french_score < 2 and english_score < 3:
            return LanguageProfile("mixed", "mixed")

    return detected


def claim_greeting(style: str | None, agent_name: str | None) -> str:
    name = (agent_name or "").strip()
    who_en = f" I'm {name} from customer support." if name else " I'm from customer support."
    who_fr = f" Je suis {name} du service client." if name else " Je suis du service client."
    who_ar = f" أنا {name} من خدمة العملاء." if name else " أنا من خدمة العملاء."
    who_leb = f" أنا {name} من خدمة الزباين." if name else " أنا من خدمة الزباين."
    who_arabizi = f" Ana {name} men customer support." if name else " Ana men customer support."

    if style == "fr":
        return f"Bonjour !{who_fr} Je vais m'occuper de votre demande à partir d'ici."
    if style == "ar":
        return f"مرحباً!{who_ar} سأتولى مساعدتك من هنا."
    if style == "leb_ar":
        return f"أهلا!{who_leb} رح كمّل معك من هون."
    if style == "leb_arabizi":
        return f"Marhaba!{who_arabizi} Rah kammel ma3ak men hon."
    if style == "mixed":
        return f"Hi!{who_arabizi} Rah kammel ma3ak from here."
    return f"Hi!{who_en} I'll be assisting you from here."


def resolution_message(style: str | None) -> str:
    if style == "fr":
        return "Merci d'avoir échangé avec notre équipe. Votre demande de support est résolue, et l'assistance automatique est de nouveau disponible si vous avez besoin d'autre chose."
    if style == "ar":
        return "شكراً لتواصلك مع فريق الدعم. تم حل طلب الدعم، وأصبحت المساعدة الآلية متاحة مجدداً إذا احتجت إلى أي شيء آخر."
    if style == "leb_ar":
        return "شكراً إنك حكيت مع فريق الدعم. خلصنا طلب الدعم، وهلّق المساعدة الآلية رجعت متاحة إذا بدك أي شي تاني."
    if style == "leb_arabizi":
        return "Merci elak 3al chat ma3 support. L support request kholset, w automated assistance sarit available marra tenye iza baddak shi."
    if style == "mixed":
        return "Thanks for chatting with support. L request kholset, w automated assistance is available again iza baddak anything else."
    return "Thanks for chatting with our support team. This support request has been resolved, and automated assistance is available again if you need anything else."


def handoff_confirmation(style: str | None) -> str:
    if style == "fr":
        return "Je vous mets en contact avec un agent du service client maintenant."
    if style == "ar":
        return "سأحوّلك الآن إلى أحد موظفي خدمة العملاء."
    if style == "leb_ar":
        return "رح وصّلك هلّق مع حدا من فريق خدمة الزباين."
    if style == "leb_arabizi":
        return "Rah wasslak halla2 ma3 hada men customer support."
    if style == "mixed":
        return "Rah wasslak halla2 ma3 a customer-support agent."
    return "I'm connecting you to a customer-support agent now."


def auth_prompt(style: str | None) -> str:
    """Step 1 of the strict two-step login flow: ask for the User ID only.

    The password must never be requested in this same message. It is requested
    separately by ``password_prompt`` only after the User ID has been received on its
    own turn.
    """
    if style == "fr":
        return "Veuillez fournir votre identifiant utilisateur."
    if style == "ar":
        return "يرجى تزويدي بمعرّف المستخدم (User ID) الخاص فيك."
    if style == "leb_ar":
        return "بعثلي user ID تبعك."
    if style == "leb_arabizi":
        return "B3atle l user ID taba3ak."
    if style == "mixed":
        return "B3atle your user ID."
    return "Please provide your User ID."


def password_prompt(style: str | None) -> str:
    if style == "fr":
        return "Merci. Envoyez maintenant votre mot de passe pour continuer."
    if style == "ar":
        return "شكراً. أرسل كلمة المرور الآن للمتابعة."
    if style == "leb_ar":
        return "تمام، بعثلي كلمة السر لنكمّل."
    if style == "leb_arabizi":
        return "Tamem, b3atle l password la nkammel."
    if style == "mixed":
        return "Tamem, send me the password la nkammel."
    return "Thanks. Please send your password to continue."


def login_confirmation(style: str | None) -> str:
    """Confirm a successful explicit login when there is no interrupted request.

    Most protected-operation logins resume the original request immediately and do
    not use this message. Keeping the remaining confirmation deterministic prevents a
    model-generated reply from switching script or blending language styles.
    """
    if style == "fr":
        return "Connexion réussie."
    if style == "ar":
        return "تم تسجيل الدخول بنجاح."
    if style == "leb_ar":
        return "تمام، صار تسجيل الدخول."
    if style == "leb_arabizi":
        return "Tamem, sar l login."
    if style == "mixed":
        return "Tamem, login successful."
    return "Login successful."


def pending_request_unavailable(style: str | None) -> str:
    """Login succeeded, but the protected request could not be completed safely."""
    if style == "fr":
        return "La connexion a réussi, mais je n'ai pas pu terminer votre demande pour le moment. Réessayez dans un instant."
    if style == "ar":
        return "تم تسجيل الدخول، لكن تعذّر إكمال طلبك حالياً. حاول مرة أخرى بعد قليل."
    if style == "leb_ar":
        return "صار تسجيل الدخول، بس ما قدرت كمّل طلبك هلّق. جرّب كمان شوي."
    if style == "leb_arabizi":
        return "Sar l login, bas ma 2dert kammel talabak halla2. Jarreb ba3den shway."
    if style == "mixed":
        return "Login succeeded, bas ma 2dert complete your request halla2. Jarreb ba3den shway."
    return "Login succeeded, but I couldn't complete your request right now. Please try again shortly."


def userid_prompt(style: str | None) -> str:
    if style == "fr":
        return "Veuillez d'abord envoyer votre identifiant utilisateur."
    if style == "ar":
        return "يرجى إرسال معرّف المستخدم أولاً."
    if style == "leb_ar":
        return "بعثلي user ID أول شي."
    if style == "leb_arabizi":
        return "B3atle l user ID awal shi."
    if style == "mixed":
        return "B3atle your user ID awal shi."
    return "Please provide your user ID first."


def auth_failure(style: str | None) -> str:
    if style == "fr":
        return "Je n'ai pas pu vérifier ces identifiants. Vérifiez votre identifiant utilisateur et votre mot de passe, puis réessayez."
    if style == "ar":
        return "تعذّر التحقق من بيانات الدخول. تأكد من معرّف المستخدم وكلمة المرور ثم حاول مجدداً."
    if style == "leb_ar":
        return "ما قدرت أتأكد من معلومات الدخول. تأكد من user ID وكلمة السر وجرّب من جديد."
    if style == "leb_arabizi":
        return "Ma 2dert et2akkad men login info. Check l user ID w l password w jarreb marra tenye."
    if style == "mixed":
        return "Ma 2dert verify l login info. Check the user ID w password w jarreb marra tenye."
    return "I couldn't verify your user ID and password. Please check your credentials and try again."


def auth_unavailable(style: str | None) -> str:
    if style == "fr":
        return "Je n'arrive pas à vérifier la connexion pour le moment. Réessayez dans un instant."
    if style == "ar":
        return "ما عم بقدر أتأكد من تسجيل الدخول هلّق. جرّب كمان مرة بعد شوي."
    if style == "leb_ar":
        return "ما عم بقدر أتأكد من تسجيل الدخول هلّق. جرّب كمان مرة بعد شوي."
    if style == "leb_arabizi":
        return "Ma 3am 2dar et2akkad men l login halla2. Jarreb marra tenye ba3d shway."
    if style == "mixed":
        return "Ma 3am 2dar verify l login halla2. Jarreb again ba3d shway."
    return "I can't verify the login right now. Please try again in a moment."


def auth_rate_limited(style: str | None) -> str:
    if style == "fr":
        return "Il y a eu trop de tentatives de connexion. Réessayez dans quelques minutes."
    if style == "ar":
        return "صار في محاولات دخول كثيرة. حاول مرة ثانية بعد كم دقيقة."
    if style == "leb_ar":
        return "صار في محاولات دخول كتير. جرّب كمان مرة بعد كم دقيقة."
    if style == "leb_arabizi":
        return "Sar fi login attempts ktir. Jarreb marra tenye ba3d kam minute."
    if style == "mixed":
        return "Sar fi too many login attempts. Jarreb marra tenye ba3d kam minute."
    return "There have been too many login attempts. Please try again in a few minutes."


def auth_clarification(style: str | None) -> str:
    """Sent when a reply contains more than one possible credential-shaped value.

    Asks for just the User ID by itself, one value at a time, matching the strict
    two-step flow — it must never ask for the User ID and password together.
    """
    if style == "fr":
        return "Je vois plusieurs valeurs possibles. Envoyez uniquement votre identifiant utilisateur, pour commencer."
    if style == "ar":
        return "في أكتر من قيمة ممكنة. ابعتلي معرّف المستخدم (User ID) لحاله بس، للبداية."
    if style == "leb_ar":
        return "في أكتر من قيمة ممكنة. بعثلي user ID لحاله بس هلق."
    if style == "leb_arabizi":
        return "Fi aktar men value momkine. B3atle bas l user ID la7alo halla2."
    if style == "mixed":
        return "Fi more than one possible value. B3atle bas your user ID la7alo halla2."
    return "I see more than one possible value there. Please send just your User ID by itself, to start."


def already_authenticated(style: str | None) -> str:
    if style == "fr":
        return "Vous êtes déjà connecté. J'ai gardé le compte actuel ; déconnectez-vous d'abord si vous voulez utiliser un autre compte."
    if style == "ar":
        return "أنت مسجّل دخول بالفعل. خليت الحساب الحالي؛ إذا بدك حساب تاني، سجّل خروج أولاً."
    if style == "leb_ar":
        return "إنت فاتت عالحساب أصلاً. خليت الحساب الحالي؛ إذا بدك تغيّره، اعمل logout أول شي."
    if style == "leb_arabizi":
        return "Enta already logged in. Khallayt l current account; iza baddak tghayro, logout awal shi."
    if style == "mixed":
        return "You're already logged in. Khallayt l current account; logout first iza baddak account tene."
    return "You're already logged in. I kept the current account; log out first if you want to use a different account."


def logout_confirmation(style: str | None) -> str:
    if style == "fr":
        return "C'est fait, vous êtes déconnecté."
    if style == "ar":
        return "تمام، تم تسجيل الخروج من الحساب."
    if style == "leb_ar":
        return "تمام، طلعتك من الحساب."
    if style == "leb_arabizi":
        return "Tamem, 3meltellak logout."
    if style == "mixed":
        return "Tamem, you're logged out."
    return "Done, you're logged out."
