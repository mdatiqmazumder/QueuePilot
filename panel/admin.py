import getpass

from argon2 import PasswordHasher
from sqlalchemy import delete, select

from .database import LoginSession, Session, User


def main():
    username = input('Admin username: ').strip()
    password = getpass.getpass('New password (minimum 12 characters): ')
    if len(password) < 12 or password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords must match and contain at least 12 characters')
    with Session.begin() as db:
        user = db.scalar(select(User).where(User.username == username))
        if not user:
            raise SystemExit('User not found')
        user.password_hash = PasswordHasher().hash(password)
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    print('Password changed and existing sessions revoked.')


if __name__ == '__main__':
    main()
