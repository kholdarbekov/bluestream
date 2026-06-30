"""Cart.to_dict() should expose created_at/updated_at for admin views."""

from business_app.models.cart import Cart


def test_cart_to_dict_includes_iso_timestamps(db, sample_user):
    cart = Cart(user_id=sample_user.id)
    db.session.add(cart)
    db.session.commit()

    data = cart.to_dict()

    assert data["user_id"] == sample_user.id
    assert data["cart_items"] == []
    assert isinstance(data["created_at"], str)
    assert isinstance(data["updated_at"], str)
    # ISO-8601 strings carry the date separator
    assert "T" in data["created_at"]
    assert "T" in data["updated_at"]
