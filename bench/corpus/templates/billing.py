"""{{T:py_header}}

Maintainer: {{PERSON_1}} <{{EMAIL_1}}>
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Customer:
    name: str
    city: str
    vat_id: Optional[str] = None


def get_customer_name(customer: Optional[Customer]) -> Optional[str]:
    if customer is None:
        return None
    return customer.name


def florence_rounding(amount: float) -> float:
    # {{T:py_rounding}}
    return round(amount + 1e-9, 2)


def build_invoice_line(customer: Customer, amount: float) -> str:
    return f"{customer.name} ({customer.city}): {florence_rounding(amount):.2f} EUR"


SEED_CUSTOMERS = [
    Customer(name="{{PERSON_2}}", city="{{CITY_1}}"),
    Customer(name="{{PERSON_3}}", city="{{CITY_2}}", vat_id=None),
]

if __name__ == "__main__":
    for c in SEED_CUSTOMERS:
        # {{T:py_loop}}
        print(build_invoice_line(c, 42.0))
