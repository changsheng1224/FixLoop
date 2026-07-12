from pricing import get_price
from discount import apply_discount

def final_price(raw, rate):
    price = get_price(raw)
    return apply_discount(price, rate)
