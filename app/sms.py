from pydantic import BaseModel, EmailStr


class OrderCreate(BaseModel):
    customer_name: str
    phone: str
    grain_type: str
    weight_kg: float
    price_per_kg: float


class OrderStatusUpdate(BaseModel):
    status: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    