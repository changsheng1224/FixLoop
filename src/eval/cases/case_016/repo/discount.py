def calculate_discount(price, discount):
    if discount > 50:
        return price * (discount / 100)
    return price * 0.9
