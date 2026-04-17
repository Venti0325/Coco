USERS = {
    1: {"name": "alice"},
    2: {"name": "bob"},
}


def find_user(uid: int):
    if uid in USERS:
        user = USERS[uid]
        # Bug: forgot to return — always None
    return None
