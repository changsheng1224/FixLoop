def apply_tax(cart, rate):
    total = 0
    for item, price in cart.items():
        total += price * (1 + rate)
    return total
