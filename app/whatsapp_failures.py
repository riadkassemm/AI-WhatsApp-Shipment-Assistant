"""Safe, actionable provider errors. Never expose raw webhook payloads."""


def failure_policy(code: int | None) -> tuple[str, bool]:
    if code == 131047:
        return ('WhatsApp 131047: This number\'s 24-hour reply window is closed. '
                'Ask the customer to message this business number before sending a new message, '
                'or use an approved Meta template. This failed attempt cannot be retried.'), False
    if isinstance(code, int) and not isinstance(code, bool) and 0 < code < 10_000_000:
        return f'WhatsApp {code}: Delivery failed. Check the provider error before retrying.', True
    return 'WhatsApp reported delivery failed. Retry is available.', True


def provider_error_code(errors: object) -> int | None:
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return None
    code = errors[0].get('code')
    return code if isinstance(code, int) and not isinstance(code, bool) and 0 < code < 10_000_000 else None
