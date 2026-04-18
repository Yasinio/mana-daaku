from datetime import date, datetime, time, timedelta
import os
import hashlib
import hmac

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, or_, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, SessionLocal, engine
from app.models import CustomerOrder
from app.schemas import OrderCreate, OrderStatusUpdate
from app.sms import (
    send_sms,
    build_booking_confirmation_sms,
    build_processing_sms,
    build_done_sms,
)

load_dotenv()

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Mana Daakuu API")

SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

templates = Jinja2Templates(directory="templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_reference_code_column():
    with engine.begin() as connection:
        inspector = inspect(connection)
        columns = [col["name"] for col in inspector.get_columns("customer_orders")]

        if "reference_code" not in columns:
            connection.execute(
                text("ALTER TABLE customer_orders ADD COLUMN reference_code VARCHAR(50)")
            )


def ensure_completed_at_column():
    with engine.begin() as connection:
        inspector = inspect(connection)
        columns = [col["name"] for col in inspector.get_columns("customer_orders")]

        if "completed_at" not in columns:
            connection.execute(
                text("ALTER TABLE customer_orders ADD COLUMN completed_at TIMESTAMP")
            )


def ensure_payment_columns():
    with engine.begin() as connection:
        inspector = inspect(connection)
        columns = [col["name"] for col in inspector.get_columns("customer_orders")]

        if "payment_status" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE customer_orders ADD COLUMN payment_status VARCHAR(20) DEFAULT 'unpaid'"
                )
            )

        if "payment_method" not in columns:
            connection.execute(
                text("ALTER TABLE customer_orders ADD COLUMN payment_method VARCHAR(20)")
            )

        connection.execute(
            text(
                """
                UPDATE customer_orders
                SET payment_status = 'unpaid'
                WHERE payment_status IS NULL
                """
            )
        )


def backfill_completed_at_for_old_done_orders():
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE customer_orders
                SET completed_at = created_at
                WHERE status = 'done' AND completed_at IS NULL AND created_at IS NOT NULL
                """
            )
        )


def backfill_created_at_for_null_orders():
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE customer_orders
                SET created_at = CURRENT_TIMESTAMP
                WHERE created_at IS NULL
                """
            )
        )


def format_reference_code(order_date: date, daily_number: int) -> str:
    return f"MD-{order_date.strftime('%Y%m%d')}-{daily_number:03d}"


def backfill_reference_codes(db: Session):
    orders = (
        db.query(CustomerOrder)
        .order_by(CustomerOrder.created_at.asc().nullslast(), CustomerOrder.id.asc())
        .all()
    )

    daily_counters = {}
    changed = False

    for order in orders:
        order_date = order.created_at.date() if order.created_at else date.today()
        date_key = order_date.strftime("%Y%m%d")

        daily_counters[date_key] = daily_counters.get(date_key, 0) + 1

        if not order.reference_code:
            order.reference_code = format_reference_code(
                order_date,
                daily_counters[date_key],
            )
            changed = True

    if changed:
        db.commit()


def assign_reference_code_to_order(db: Session, order: CustomerOrder) -> str:
    if order.reference_code:
        return order.reference_code

    order_date = order.created_at.date() if order.created_at else date.today()
    start_dt = datetime.combine(order_date, time.min)
    end_dt = start_dt + timedelta(days=1)

    same_day_orders = (
        db.query(CustomerOrder)
        .filter(
            CustomerOrder.created_at >= start_dt,
            CustomerOrder.created_at < end_dt,
        )
        .order_by(CustomerOrder.created_at.asc(), CustomerOrder.id.asc())
        .all()
    )

    daily_number = 1
    for index, existing_order in enumerate(same_day_orders, start=1):
        if existing_order.id == order.id:
            daily_number = index
            break

    order.reference_code = format_reference_code(order_date, daily_number)
    db.commit()
    db.refresh(order)

    return order.reference_code


def get_active_orders(db: Session):
    return (
        db.query(CustomerOrder)
        .filter(CustomerOrder.status.in_(["booked", "processing"]))
        .order_by(CustomerOrder.created_at.asc().nullslast(), CustomerOrder.id.asc())
        .all()
    )


def get_queue_position_and_wait(db: Session, order_id: int):
    active_orders = get_active_orders(db)

    for index, order in enumerate(active_orders, start=1):
        if order.id == order_id:
            queue_position = index
            estimated_wait_minutes = (index - 1) * 15
            return queue_position, estimated_wait_minutes

    return None, None


def send_booking_sms(order: CustomerOrder, db: Session):
    _, wait_minutes = get_queue_position_and_wait(db, order.id)
    if wait_minutes is None:
        wait_minutes = 0

    sms_message = build_booking_confirmation_sms(order, wait_minutes)
    send_sms(order.phone, sms_message)


def send_processing_sms(order: CustomerOrder, db: Session):
    _, wait_minutes = get_queue_position_and_wait(db, order.id)
    if wait_minutes is None:
        wait_minutes = 0

    sms_message = build_processing_sms(order, wait_minutes)
    send_sms(order.phone, sms_message)


def send_done_sms(order: CustomerOrder):
    sms_message = build_done_sms(order)
    send_sms(order.phone, sms_message)


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("admin_logged_in"))


def verify_password(plain_password: str, stored_password_hash: str) -> bool:
    try:
        salt_hex, hash_hex = stored_password_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)

        test_hash = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            salt,
            100_000,
        )

        return hmac.compare_digest(test_hash, expected_hash)
    except Exception:
        return False


def find_possible_duplicate_order(
    db: Session,
    customer_name: str,
    phone: str,
    grain_type: str,
    weight_kg: float,
    price_per_kg: float,
):
    return (
        db.query(CustomerOrder)
        .filter(CustomerOrder.status.in_(["booked", "processing"]))
        .filter(CustomerOrder.customer_name == customer_name)
        .filter(CustomerOrder.phone == phone)
        .filter(CustomerOrder.grain_type == grain_type)
        .filter(CustomerOrder.weight_kg == weight_kg)
        .filter(CustomerOrder.price_per_kg == price_per_kg)
        .order_by(CustomerOrder.created_at.desc().nullslast(), CustomerOrder.id.desc())
        .first()
    )


def create_new_order(
    db: Session,
    customer_name: str,
    phone: str,
    grain_type: str,
    weight_kg: float,
    price_per_kg: float,
    payment_status: str = "paid",
    payment_method: str | None = None,
):
    total_price = weight_kg * price_per_kg

    new_order = CustomerOrder(
        customer_name=customer_name,
        phone=phone,
        grain_type=grain_type,
        weight_kg=weight_kg,
        price_per_kg=price_per_kg,
        total_price=total_price,
        payment_status=payment_status,
        payment_method=payment_method,
        created_at=datetime.utcnow(),
    )

    db.add(new_order)
    db.commit()
    db.refresh(new_order)

    reference_code = assign_reference_code_to_order(db, new_order)
    send_booking_sms(new_order, db)

    return new_order, reference_code, total_price


@app.on_event("startup")
def startup_tasks():
    ensure_reference_code_column()
    ensure_completed_at_column()
    ensure_payment_columns()
    backfill_created_at_for_null_orders()

    db = SessionLocal()
    try:
        backfill_reference_codes(db)
    finally:
        db.close()

    backfill_completed_at_for_old_done_orders()


@app.get("/", response_class=HTMLResponse)
def booking_page(request: Request):
    return templates.TemplateResponse(
        request,
        "booking.html",
        {
            "success_message": None,
            "success_reference": None,
            "error_message": None,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/dashboard", status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error_message": None,
        },
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if username == ADMIN_USERNAME and verify_password(password, ADMIN_PASSWORD_HASH):
        request.session["admin_logged_in"] = True
        request.session["admin_username"] = username
        return RedirectResponse(url="/dashboard", status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error_message": "Invalid username or password.",
        },
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.post("/submit-booking", response_class=HTMLResponse)
def submit_booking(
    request: Request,
    customer_name: str = Form(...),
    phone: str = Form(...),
    grain_type: str = Form(...),
    weight_kg: float = Form(...),
    price_per_kg: float = Form(...),
    payment_method: str = Form(...),
    payment_confirmed: str = Form(...),
    db: Session = Depends(get_db),
):
    if payment_method not in ["cash", "transfer", "mobile"]:
        return templates.TemplateResponse(
            request,
            "booking.html",
            {
                "success_message": None,
                "success_reference": None,
                "error_message": "Please select a valid payment method.",
            },
        )

    if payment_confirmed != "yes":
        return templates.TemplateResponse(
            request,
            "booking.html",
            {
                "success_message": None,
                "success_reference": None,
                "error_message": "Payment must be confirmed before booking is submitted.",
            },
        )

    duplicate_order = find_possible_duplicate_order(
        db=db,
        customer_name=customer_name,
        phone=phone,
        grain_type=grain_type,
        weight_kg=weight_kg,
        price_per_kg=price_per_kg,
    )

    if duplicate_order:
        return templates.TemplateResponse(
            request,
            "confirm_duplicate.html",
            {
                "customer_name": customer_name,
                "phone": phone,
                "grain_type": grain_type,
                "weight_kg": weight_kg,
                "price_per_kg": price_per_kg,
                "payment_method": payment_method,
                "payment_confirmed": payment_confirmed,
                "duplicate_order": duplicate_order,
            },
        )

    _, reference_code, total_price = create_new_order(
        db=db,
        customer_name=customer_name,
        phone=phone,
        grain_type=grain_type,
        weight_kg=weight_kg,
        price_per_kg=price_per_kg,
        payment_status="paid",
        payment_method=payment_method,
    )

    return templates.TemplateResponse(
        request,
        "booking.html",
        {
            "success_message": f"Booking created successfully for {customer_name}. Total price: {total_price}. Payment recorded as paid.",
            "success_reference": reference_code,
            "error_message": None,
        },
    )


@app.post("/submit-booking/confirm", response_class=HTMLResponse)
def confirm_duplicate_booking(
    request: Request,
    customer_name: str = Form(...),
    phone: str = Form(...),
    grain_type: str = Form(...),
    weight_kg: float = Form(...),
    price_per_kg: float = Form(...),
    payment_method: str = Form("cash"),
    payment_confirmed: str = Form("yes"),
    confirm_duplicate: str = Form(...),
    db: Session = Depends(get_db),
):
    if confirm_duplicate == "no":
        return templates.TemplateResponse(
            request,
            "booking.html",
            {
                "success_message": None,
                "success_reference": None,
                "error_message": "Duplicate booking was cancelled.",
            },
        )

    if payment_method not in ["cash", "transfer", "mobile"]:
        payment_method = "cash"

    if payment_confirmed != "yes":
        return templates.TemplateResponse(
            request,
            "booking.html",
            {
                "success_message": None,
                "success_reference": None,
                "error_message": "Payment must be confirmed before booking.",
            },
        )

    _, reference_code, total_price = create_new_order(
        db=db,
        customer_name=customer_name,
        phone=phone,
        grain_type=grain_type,
        weight_kg=weight_kg,
        price_per_kg=price_per_kg,
        payment_status="paid",
        payment_method=payment_method,
    )

    return templates.TemplateResponse(
        request,
        "booking.html",
        {
            "success_message": f"Booking created successfully for {customer_name}. Total price: {total_price}. Payment recorded as PAID.",
            "success_reference": reference_code,
            "error_message": None,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def owner_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    search: str = Query(default=""),
    status_filter: str = Query(default="all"),
    payment_filter: str = Query(default="all"),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today()

    query = db.query(CustomerOrder)

    cleaned_search = search.strip()

    if cleaned_search:
        query = query.filter(
            or_(
                CustomerOrder.customer_name.ilike(f"%{cleaned_search}%"),
                CustomerOrder.phone.ilike(f"%{cleaned_search}%"),
                CustomerOrder.reference_code.ilike(f"%{cleaned_search}%"),
            )
        )

    if status_filter != "all":
        query = query.filter(CustomerOrder.status == status_filter)

    if payment_filter != "all":
        query = query.filter(CustomerOrder.payment_status == payment_filter)

    filtered_orders = (
        query.order_by(CustomerOrder.created_at.asc().nullslast(), CustomerOrder.id.asc()).all()
    )

    all_orders_for_summary = (
        db.query(CustomerOrder)
        .order_by(CustomerOrder.created_at.asc().nullslast(), CustomerOrder.id.asc())
        .all()
    )

    active_orders = [
        order for order in filtered_orders if order.status in ["booked", "processing"]
    ]

    queue_list = []
    for index, order in enumerate(active_orders, start=1):
        queue_list.append(
            {
                "queue_position": index,
                "estimated_wait_minutes": (index - 1) * 15,
                "id": order.id,
                "reference_code": order.reference_code,
                "customer_name": order.customer_name,
                "phone": order.phone,
                "grain_type": order.grain_type,
                "weight_kg": order.weight_kg,
                "price_per_kg": order.price_per_kg,
                "total_price": order.total_price,
                "status": order.status,
                "payment_status": order.payment_status,
                "payment_method": order.payment_method,
                "created_at": order.created_at,
                "completed_at": order.completed_at,
            }
        )

    today_orders = [
        order
        for order in all_orders_for_summary
        if order.created_at
        and order.created_at.date() == today
        and order.status != "cancelled"
    ]

    today_paid_orders = [
        order for order in today_orders if order.payment_status == "paid"
    ]

    total_income = sum(order.total_price for order in today_paid_orders)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "today_date": str(today),
            "today_orders_count": len(today_orders),
            "paid_today_orders_count": len(today_paid_orders),
            "total_income": total_income,
            "queue_list": queue_list,
            "all_orders": filtered_orders,
            "admin_username": request.session.get("admin_username", "admin"),
            "search": cleaned_search,
            "status_filter": status_filter,
            "payment_filter": payment_filter,
        },
    )


@app.post("/dashboard/update-status/{order_id}")
def dashboard_update_status(
    request: Request,
    order_id: int,
    new_status: str = Form(...),
    db: Session = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    old_status = order.status

    if old_status == new_status:
        return RedirectResponse(url="/dashboard", status_code=303)

    order.status = new_status

    if new_status == "done":
        order.completed_at = datetime.utcnow()
    else:
        order.completed_at = None

    db.commit()
    db.refresh(order)

    if new_status == "processing":
        send_processing_sms(order, db)
    elif new_status == "done":
        send_done_sms(order)

    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/dashboard/payment/{order_id}")
def update_payment_status(
    request: Request,
    order_id: int,
    payment_status: str = Form(...),
    payment_method: str = Form(default=""),
    db: Session = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status == "cancelled":
        return RedirectResponse(url="/dashboard", status_code=303)

    if payment_status == "paid":
        if payment_method not in ["cash", "transfer", "mobile"]:
            return RedirectResponse(url="/dashboard", status_code=303)

        order.payment_status = "paid"
        order.payment_method = payment_method
    else:
        order.payment_status = "unpaid"
        order.payment_method = None

    db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/dashboard/cancel-order/{order_id}", response_class=HTMLResponse)
def cancel_order_confirm_page(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return templates.TemplateResponse(
        request,
        "confirm_cancel.html",
        {
            "order": order,
            "admin_username": request.session.get("admin_username", "admin"),
        },
    )


@app.post("/dashboard/cancel-order/{order_id}")
def cancel_order(
    request: Request,
    order_id: int,
    confirm_cancel: str = Form(...),
    db: Session = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    if confirm_cancel == "no":
        return RedirectResponse(url="/dashboard", status_code=303)

    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status != "done":
        order.status = "cancelled"
        order.completed_at = None
        order.payment_status = "unpaid"
        order.payment_method = None
        db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/dashboard/delete-order/{order_id}", response_class=HTMLResponse)
def delete_order_confirm_page(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return templates.TemplateResponse(
        request,
        "confirm_delete.html",
        {
            "order": order,
            "admin_username": request.session.get("admin_username", "admin"),
        },
    )


@app.post("/dashboard/delete-order/{order_id}")
def delete_order(
    request: Request,
    order_id: int,
    confirm_delete: str = Form(...),
    db: Session = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    if confirm_delete == "no":
        return RedirectResponse(url="/dashboard", status_code=303)

    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    db.delete(order)
    db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    search: str = "",
    db: Session = Depends(get_db),
):
    customer_order = None
    queue_position = None
    estimated_wait_minutes = None
    active_queue_count = 0
    message = None

    active_orders = get_active_orders(db)
    active_queue_count = len(active_orders)
    cleaned_search = search.strip()

    if cleaned_search:
        latest_order = (
            db.query(CustomerOrder)
            .filter(
                or_(
                    CustomerOrder.phone == cleaned_search,
                    CustomerOrder.reference_code == cleaned_search,
                )
            )
            .order_by(CustomerOrder.created_at.desc().nullslast(), CustomerOrder.id.desc())
            .first()
        )

        if latest_order:
            customer_order = latest_order

            if latest_order.status in ["booked", "processing"]:
                queue_position, estimated_wait_minutes = get_queue_position_and_wait(
                    db, latest_order.id
                )
            elif latest_order.status == "done":
                queue_position = "Completed"
                estimated_wait_minutes = 0
            elif latest_order.status == "cancelled":
                queue_position = "Cancelled"
                estimated_wait_minutes = 0
        else:
            message = "No booking found for that phone number or reference code."

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "search": cleaned_search,
            "customer_order": customer_order,
            "queue_position": queue_position,
            "estimated_wait_minutes": estimated_wait_minutes,
            "active_queue_count": active_queue_count,
            "message": message,
        },
    )


@app.get("/income/today", response_class=HTMLResponse)
def income_today_page(request: Request, db: Session = Depends(get_db)):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    today = date.today()
    tomorrow = today + timedelta(days=1)

    today_orders = (
        db.query(CustomerOrder)
        .filter(
            CustomerOrder.created_at >= datetime.combine(today, time.min),
            CustomerOrder.created_at < datetime.combine(tomorrow, time.min),
        )
        .order_by(CustomerOrder.created_at.desc(), CustomerOrder.id.desc())
        .all()
    )

    today_orders = [order for order in today_orders if order.status != "cancelled"]

    paid_orders = [order for order in today_orders if order.payment_status == "paid"]
    unpaid_orders = [order for order in today_orders if order.payment_status == "unpaid"]
    done_orders = [order for order in today_orders if order.status == "done"]

    total_income = sum(order.total_price for order in paid_orders)

    return templates.TemplateResponse(
        request,
        "income_today.html",
        {
            "today_date": str(today),
            "today_orders": len(today_orders),
            "paid_orders": len(paid_orders),
            "unpaid_orders": len(unpaid_orders),
            "done_orders_count": len(done_orders),
            "total_income": total_income,
            "income_orders": today_orders,
            "is_admin": True,
        },
    )


@app.get("/api")
def home():
    return {"message": "Mana Daakuu backend is running"}


@app.post("/orders")
def create_order(order: OrderCreate, db: Session = Depends(get_db)):
    _, reference_code, total_price = create_new_order(
        db=db,
        customer_name=order.customer_name,
        phone=order.phone,
        grain_type=order.grain_type,
        weight_kg=order.weight_kg,
        price_per_kg=order.price_per_kg,
        payment_status="paid",
        payment_method="cash",
    )

    return {
        "message": "Order created successfully",
        "reference_code": reference_code,
        "status": "booked",
        "payment_status": "paid",
        "payment_method": "cash",
        "total_price": total_price,
    }


@app.get("/orders")
def get_orders(db: Session = Depends(get_db)):
    orders = (
        db.query(CustomerOrder)
        .order_by(CustomerOrder.created_at.desc().nullslast(), CustomerOrder.id.desc())
        .all()
    )

    return [
        {
            "id": order.id,
            "reference_code": order.reference_code,
            "customer_name": order.customer_name,
            "phone": order.phone,
            "grain_type": order.grain_type,
            "weight_kg": order.weight_kg,
            "price_per_kg": order.price_per_kg,
            "total_price": order.total_price,
            "status": order.status,
            "payment_status": order.payment_status,
            "payment_method": order.payment_method,
            "created_at": order.created_at,
            "completed_at": order.completed_at,
        }
        for order in orders
    ]


@app.put("/orders/{order_id}/status")
def update_order_status(
    order_id: int,
    status_update: OrderStatusUpdate,
    db: Session = Depends(get_db),
):
    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    old_status = order.status
    new_status = status_update.status

    if old_status == new_status:
        return {
            "message": "Order status unchanged",
            "order_id": order.id,
            "reference_code": order.reference_code,
            "new_status": order.status,
            "completed_at": order.completed_at,
            "payment_status": order.payment_status,
            "payment_method": order.payment_method,
        }

    order.status = new_status

    if new_status == "done":
        order.completed_at = datetime.utcnow()
    else:
        order.completed_at = None

    db.commit()
    db.refresh(order)

    if new_status == "processing":
        send_processing_sms(order, db)
    elif new_status == "done":
        send_done_sms(order)

    return {
        "message": "Order status updated successfully",
        "order_id": order.id,
        "reference_code": order.reference_code,
        "new_status": order.status,
        "completed_at": order.completed_at,
        "payment_status": order.payment_status,
        "payment_method": order.payment_method,
    }


@app.get("/api/queue")
def get_queue(db: Session = Depends(get_db)):
    active_orders = get_active_orders(db)

    queue_list = []
    for index, order in enumerate(active_orders, start=1):
        queue_list.append(
            {
                "queue_position": index,
                "estimated_wait_minutes": (index - 1) * 15,
                "id": order.id,
                "reference_code": order.reference_code,
                "customer_name": order.customer_name,
                "phone": order.phone,
                "grain_type": order.grain_type,
                "weight_kg": order.weight_kg,
                "price_per_kg": order.price_per_kg,
                "total_price": order.total_price,
                "status": order.status,
                "payment_status": order.payment_status,
                "payment_method": order.payment_method,
                "created_at": order.created_at,
                "completed_at": order.completed_at,
            }
        )

    return queue_list


@app.get("/api/income/today")
def get_today_income(request: Request, db: Session = Depends(get_db)):
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Not authenticated")

    today = date.today()
    tomorrow = today + timedelta(days=1)

    today_orders = (
        db.query(CustomerOrder)
        .filter(
            CustomerOrder.created_at >= datetime.combine(today, time.min),
            CustomerOrder.created_at < datetime.combine(tomorrow, time.min),
        )
        .all()
    )

    today_orders = [order for order in today_orders if order.status != "cancelled"]
    paid_orders = [order for order in today_orders if order.payment_status == "paid"]
    unpaid_orders = [order for order in today_orders if order.payment_status == "unpaid"]
    total_income = sum(order.total_price for order in paid_orders)

    return {
        "date": str(today),
        "today_orders": len(today_orders),
        "paid_orders": len(paid_orders),
        "unpaid_orders": len(unpaid_orders),
        "total_income": total_income,
    }

