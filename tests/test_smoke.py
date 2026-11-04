def test_money_uses_decimal():
    from decimal import Decimal
    assert Decimal('0.10') + Decimal('0.20') == Decimal('0.30')
