def test_relay_contract():
    for index in range(300):
        print(f"relay noise {index:03d} " + "x" * 45)
    assert False, "The relay rejected a valid frame"
