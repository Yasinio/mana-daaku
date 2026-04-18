def format_phone_number(phone: str) -> str:
    phone = phone.strip().replace(" ", "")

    if phone.startswith("+"):
        return phone
    if phone.startswith("0"):
        return "+251" + phone[1:]
    if phone.startswith("251"):
        return "+" + phone

    return phone


def send_sms(to_phone: str, message: str):
    formatted_phone = format_phone_number(to_phone)

    print("\n===================================")
    print(f"FAKE SMS TO: {formatted_phone}")
    print(f"MESSAGE: {message}")
    print("===================================\n")

    return "fake-sms-id"


def build_booking_confirmation_sms(order, wait_minutes: int) -> str:
    return (
        f"Mana Daakuu: Booking confirmed. "
        f"Ref {order.reference_code}. "
        f"Wait about {wait_minutes} min."
    )


def build_processing_sms(order, wait_minutes: int) -> str:
    if wait_minutes <= 0:
        return f"Mana Daakuu: Your order {order.reference_code} is now being processed."
    return (
        f"Mana Daakuu: Your order {order.reference_code} is now being processed. "
        f"Estimated remaining wait: {wait_minutes} min."
    )


def build_done_sms(order) -> str:
    return f"Mana Daakuu: Order {order.reference_code} is ready for pickup."

