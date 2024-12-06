from app.core.database import database
from app.repository.base_repository import BaseRepository


class TransactionRepository(BaseRepository):
    def __init__(self):
        super().__init__(database.get_collection("transactions"))