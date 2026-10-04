from project.api.passwords import hash_password, verify_password


def test_password_hash_is_salted_and_verifies_only_the_original() -> None:
    password = "a sufficiently long test phrase"
    first = hash_password(password)
    second = hash_password(password)

    assert first.startswith("$argon2id$")
    assert first != password
    assert first != second
    assert verify_password(password, first)
    assert verify_password(password, second)
    assert not verify_password("a different test phrase", first)
