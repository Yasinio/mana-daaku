from datetime import date, datetime, time, timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, or_, text
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import CustomerOrder
from app.schemas import OrderCreate, OrderStatusUpdate

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Mana Daakuu API")
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


def backfill_completed_at_for_old_done_orders():
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE customer_orders
                SET completed_at = created_at
                WHERE status = 'done' AND completed_at IS NULL
                """
            )
        )


def format_reference_code(order_date: date, daily_number: int) -> str:
    return f"MD-{order_date.strftime('%Y%m%d')}-{daily_number:03d}"


def backfill_reference_codes(db: Session):
    orders = (
        db.query(CustomerOrder)
        .order_by(CustomerOrder.created_at.asc(), CustomerOrder.id.asc())
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


@app.on_event("startup")
def startup_tasks():
    ensure_reference_code_column()
    ensure_completed_at_column()

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
        },
    )


@app.post("/submit-booking", response_class=HTMLResponse)
def submit_booking(
    request: Request,
    customer_name: str = Form(...),
    phone: str = Form(...),
    grain_type: str = Form(...),
    weight_kg: float = Form(...),
    price_per_kg: float = Form(...),
    db: Session = Depends(get_db),
):
    total_price = weight_kg * price_per_kg

    new_order = CustomerOrder(
        customer_name=customer_name,
        phone=phone,
        grain_type=grain_type,
        weight_kg=weight_kg,
        price_per_kg=price_per_kg,
        total_price=total_price,
    )
    db.add(new_order)
    db.commit()
    db.refresh(new_order)

    reference_code = assign_reference_code_to_order(db, new_order)

    return templates.TemplateResponse(
        request,
        "booking.html",
        {
            "success_message": f"Booking created successfully for {customer_name}. Total price: {total_price}",
            "success_reference": reference_code,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def owner_dashboard(request: Request, db: Session = Depends(get_db)):
    today = date.today()

    all_orders = (
        db.query(CustomerOrder)
        .order_by(CustomerOrder.created_at.asc(), CustomerOrder.id.asc())
        .all()
    )

    active_orders = [
        order for order in all_orders if order.status in ["booked", "processing"]
    ]

    queue_list = []
    for index, order in enumerate(active_orders, start=1):
        queue_list.append(
            {
                "queue_position": index,
                "id": order.id,
                "reference_code": order.reference_code,
                "customer_name": order.customer_name,
                "phone": order.phone,
                "grain_type": order.grain_type,
                "weight_kg": order.weight_kg,
                "price_per_kg": order.price_per_kg,
                "total_price": order.total_price,
                "status": order.status,
                "created_at": order.created_at,
                "completed_at": order.completed_at,
            }
        )

    today_done_orders = [
        order
        for order in all_orders
        if order.completed_at
        and order.completed_at.date() == today
        and order.status == "done"
    ]

    total_income = sum(order.total_price for order in today_done_orders)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "today_date": str(today),
            "completed_orders": len(today_done_orders),
            "total_income": total_income,
            "queue_list": queue_list,
            "all_orders": all_orders,
        },
    )


@app.post("/dashboard/update-status/{order_id}")
def dashboard_update_status(
    order_id: int,
    new_status: str = Form(...),
    db: Session = Depends(get_db),
):
    order = db.query(CustomerOrder).filter(CustomerOrder.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = new_status

    if new_status == "done":
        order.completed_at = datetime.utcnow()
    else:
        order.completed_at = None

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

    active_orders = (
        db.query(CustomerOrder)
        .filter(CustomerOrder.status.in_(["booked", "processing"]))
        .order_by(CustomerOrder.created_at.asc(), CustomerOrder.id.asc())
        .all()
    )

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
            .order_by(CustomerOrder.created_at.desc(), CustomerOrder.id.desc())
            .first()
        )

        if latest_order:
            customer_order = latest_order

            if latest_order.status in ["booked", "processing"]:
                for index, order in enumerate(active_orders, start=1):
                    if order.id == latest_order.id:
                        queue_position = index
                        estimated_wait_minutes = (index - 1) * 15
                        break
            else:
                queue_position = "Completed"
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
    today = date.today()

    today_done_orders = (
        db.query(CustomerOrder)
        .filter(CustomerOrder.status == "done")
        .order_by(CustomerOrder.completed_at.desc(), CustomerOrder.id.desc())
        .all()
    )

    today_done_orders = [
        order
        for order in today_done_orders
        if order.completed_at and order.completed_at.date() == today
    ]

    total_income = sum(order.total_price for order in today_done_orders)

    return templates.TemplateResponse(
        request,
        "income_today.html",
        {
            "today_date": str(today),
            "completed_orders": len(today_done_orders),
            "total_income": total_income,
            "done_orders": today_done_orders,
        },
    )


@app.get("/api")
def home():
    return {"message": "Mana Daakuu backend is running"}


@app.post("/orders")
def create_order(order: OrderCreate, db: Session = Depends(get_db)):
    total_price = order.weight_kg * order.price_per_kg

    new_order = CustomerOrder(
        customer_name=order.customer_name,
        phone=order.phone,
        grain_type=order.grain_type,
        weight_kg=order.weight_kg,
        price_per_kg=order.price_per_kg,
        total_price=total_price,
    )
    db.add(new_order)
    db.commit()
    db.refresh(new_order)

    reference_code = assign_reference_code_to_order(db, new_order)

    return {
        "message": "Order created successfully",
        "order_id": new_order.id,
        "reference_code": reference_code,
        "status": new_order.status,
        "total_price": new_order.total_price,
    }


@app.get("/orders")
def get_orders(db: Session = Depends(get_db)):
    orders = db.query(CustomerOrder).all()

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

    order.status = status_update.status

    if status_update.status == "done":
        order.completed_at = datetime.utcnow()
    else:
        order.completed_at = None

    db.commit()
    db.refresh(order)

    return {
        "message": "Order status updated successfully",
        "order_id": order.id,
        "reference_code": order.reference_code,
        "new_status": order.status,
        "completed_at": order.completed_at,
    }


@app.get("/api/queue")
def get_queue(db: Session = Depends(get_db)):
    active_orders = (
        db.query(CustomerOrder)
        .filter(CustomerOrder.status.in_(["booked", "processing"]))
        .order_by(CustomerOrder.created_at.asc(), CustomerOrder.id.asc())
        .all()
    )

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
                "created_at": order.created_at,
                "completed_at": order.completed_at,
            }
        )

    return queue_list


@app.get("/api/income/today")
def get_today_income(db: Session = Depends(get_db)):
    today = date.today()

    completed_orders = db.query(CustomerOrder).all()

    today_done_orders = [
        order
        for order in completed_orders
        if order.completed_at
        and order.completed_at.date() == today
        and order.status == "done"
    ]

    total_income = sum(order.total_price for order in today_done_orders)

    return {
        "date": str(today),
        "completed_orders": len(today_done_orders),
        "total_income": total_income,
    }

