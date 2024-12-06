import hashlib
import hmac
import math
from datetime import datetime
from typing import Tuple

import requests
from bson import ObjectId
from fastapi import HTTPException
from pymongo.results import UpdateResult

from app.model.photo_model import SellPhoto, StatusSellPhoto

from app.core.config import config
from app.core.security import get_encoded_server_key
from app.model.transaction_model import Transaction, Payment
from app.repository.photo_repository import PhotoRepository
from app.repository.transaction_repository import TransactionRepository
from app.repository.user_repository import UserRepository
from app.schema.transaction_schema import TransactionRequest, TransactionResponse, PaymentMidtransRequest, \
    GetTransactionRequest, GetPaymentRequest, VerifySignatureRequest, TransactionStatus, ListTransactionRequest
from app.core.logger import logger
from typing import List

class TransactionService:
    def __init__(self):
        self.transaction_repository = TransactionRepository()
        self.photo_repository = PhotoRepository()
        self.user_repository = UserRepository()
        self.server_key = get_encoded_server_key()
        self.url = config.url_sandbox

    def create(self, request: TransactionRequest) -> TransactionResponse:
        errors = {}
        required_fields = {
            "buyer_id": "buyer ID is required",
            "details": "details are required",
            "total": "total is required",
        }

        for field, error_message in required_fields.items():
            if not request.dict().get(field):
                errors[field] = error_message

        if errors:
            logger.error(f"Validation error: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        try:
            photo_update_results = []
            buyer = self.user_repository.find_by_id(ObjectId(request.buyer_id))
            if not buyer:
                raise HTTPException(status_code=404, detail="User not found")
            for detail in request.details:
                seller = self.user_repository.find_by_id(ObjectId(detail.seller_id), include=["_id"])
                if not seller:
                    raise HTTPException(status_code=404, detail="Seller not found")
                for photo_id in detail.photo_id:
                    photo = self.photo_repository.find_by_sold(ObjectId(photo_id))
                    if photo is None:
                        errors[f"{photo_id}"] = "Photo not found or already sold"
                    else:
                        if photo["user_id"] != seller["_id"]:
                            raise HTTPException(status_code=400, detail="Photo not owned by seller")
                        photo["status"] = StatusSellPhoto.WAITING
                        photo["buyer_id"] = ObjectId(request.buyer_id)
                        photo["updated_at"] = datetime.now()
                        photo_update_results.append(photo)
            if errors:
                logger.error(f"Validation error: {errors}")
                raise HTTPException(status_code=400, detail=errors)

            transaction = Transaction(**request.dict())
            result = self.transaction_repository.create(transaction)
            transaction = self.transaction_repository.find_by_id(result.inserted_id)
            logger.info(f"Transaction created: {transaction}")
            for photo in photo_update_results:
                photo = SellPhoto(**photo)
                update_photo = self.photo_repository.update(photo)
                logger.info(f"Photo updated: {update_photo}")

            payment = self.qris_payment(transaction)
            payment_payload = {
                "_id": payment["transaction_id"],
                "status": payment["transaction_status"],
                "url": payment["actions"][0]["url"],
                "expired_at": payment["expiry_time"]
            }
            transaction["payment"] = Payment(**payment_payload)
            transaction = Transaction(**transaction)
            update_Result: UpdateResult = self.transaction_repository.update(transaction)
            if update_Result.modified_count == 0:
                raise HTTPException(status_code=500, detail="Failed to update transaction")
            updated_transaction = self.transaction_repository.find_by_id(result.inserted_id)
            updated_transaction["_id"] = str(updated_transaction["_id"])
            updated_transaction["buyer_id"] = str(updated_transaction["buyer_id"])
            for detail in updated_transaction["details"]:
                detail["seller_id"] = str(detail["seller_id"])
                detail["photo_id"] = [str(pid) for pid in detail["photo_id"]]
            return TransactionResponse(**updated_transaction)

        except Exception as e:
            logger.error(f"Error when creating transaction: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    def get(self, request: GetTransactionRequest):
        errors = {}
        required_fields = {
            "id": "transaction ID is required",
            "user_id": "user ID is required"
        }

        for field, error_message in required_fields.items():
            if not request.dict().get(field):
                errors[field] = error_message

        if errors:
            logger.error(f"Validation error: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        try:
            transaction = self.transaction_repository.find_by_id(ObjectId(request.id))
            if not transaction:
                raise HTTPException(status_code=404, detail="Transaction not found")
            transaction["_id"] = str(transaction["_id"])
            transaction["buyer_id"] = str(transaction["buyer_id"])
            for detail in transaction["details"]:
                detail["seller_id"] = str(detail["seller_id"])
                detail["photo_id"] = [str(pid) for pid in detail["photo_id"]]
            return TransactionResponse(**transaction)
        except Exception as e:
            logger.error(f"Error when getting transaction: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    def list_by_buyer(self, request: ListTransactionRequest) -> Tuple[List[dict], int]:
        try:
            transactions, total = self.transaction_repository.list_by_buyer(request)
            for transaction in transactions:
                transaction["_id"] = str(transaction["_id"])
                transaction["buyer_id"] = str(transaction["buyer_id"])
                for detail in transaction["details"]:
                    detail["seller_id"] = str(detail["seller_id"])
                    detail["photo_id"] = [str(pid) for pid in detail["photo_id"]]
            return transactions, total
        except Exception as e:
            logger.error(f"Error when listing transaction: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    def list_by_seller(self, request: ListTransactionRequest) -> Tuple[List[dict], int]:
        try:
            transactions, total = self.transaction_repository.list_by_seller(request)
            logger.info(f"Transactions: {transactions}")
            data = []
            for transaction in transactions:
                transaction["_id"] = str(transaction["_id"])
                transaction["buyer_id"] = str(transaction["buyer_id"])
                for detail in transaction["details"]:
                    detail["seller_id"] = str(detail["seller_id"])
                    detail["photo_id"] = [str(pid) for pid in detail["photo_id"]]
                data.append(TransactionResponse(**transaction).dict(by_alias=True))
            return data, total
        except Exception as e:
            logger.error(f"Error when listing transaction: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    def get_payment(self, request: GetPaymentRequest):
        errors = {}
        required_fields = {
            "id": "transaction ID is required",
            "user_id": "user ID is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field, None):
                errors[field] = error_message

        if errors:
            logger.error(f"Validation error: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        try:
            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "Authorization": f"Basic {self.server_key}:"
            }

            response = requests.get(f"{self.url}{request.id}/status", headers=headers)
            logger.info(f"Get payment response: {response.json()}")
            return response.json()
        except Exception as e:
            logger.error(f"Error when getting payment: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    def verify_payment(self, request: VerifySignatureRequest, payload: dict) -> TransactionResponse:
        logger.info(f"Payload: {request.dict()}")
        data = f"{request.order_id}{request.status_code}{request.gross_amount}{config.server_key_sandbox if config.app_env == "local" else config.server_key_production}"
        data_encode = data.encode()
        calculate_signature = hashlib.sha512(data_encode).hexdigest()
        logger.info(f"Calculated signature: {calculate_signature}")
        if calculate_signature != request.signature:
            raise HTTPException(status_code=400, detail="Invalid signature")

        order_id = payload.get("order_id")
        transaction_status = payload.get("transaction_status")

        transaction = self.transaction_repository.find_by_id(ObjectId(order_id))
        if transaction_status == "settlement":
            transaction["status"] = TransactionStatus.PAID
            transaction["updated_at"] = datetime.now()
            transaction["payment"]["status"] = transaction_status
        elif transaction_status == "expire":
            transaction["status"] = TransactionStatus.EXPIRED
            transaction["updated_at"] = datetime.now()
            transaction["payment"]["status"] = transaction_status
        elif transaction_status == "cancel":
            transaction["status"] = TransactionStatus.CANCELLED
            transaction["updated_at"] = datetime.now()
            transaction["payment"]["status"] = transaction_status
        elif transaction_status == "deny":
            transaction["status"] = TransactionStatus.CANCELLED
            transaction["updated_at"] = datetime.now()
            transaction["payment"]["status"] = transaction_status
        elif transaction_status == "pending":
            transaction["status"] = TransactionStatus.PENDING
            transaction["updated_at"] = datetime.now()
            transaction["payment"]["status"] = transaction_status
        else:
            logger.error(f"Unknown status for Order ID: {order_id}")
            raise HTTPException(status_code=400, detail="Invalid transaction status")

        update_result: UpdateResult = self.transaction_repository.update(Transaction(**transaction))
        if update_result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to update transaction")
        updated_transaction = self.transaction_repository.find_by_id(ObjectId(order_id))
        updated_transaction["_id"] = str(updated_transaction["_id"])
        updated_transaction["buyer_id"] = str(updated_transaction["buyer_id"])
        for detail in updated_transaction["details"]:
            detail["seller_id"] = str(detail["seller_id"])
            detail["photo_id"] = [str(pid) for pid in detail["photo_id"]]
        return TransactionResponse(**updated_transaction)

    def qris_payment(self, transaction):
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "Authorization": f"Basic {self.server_key}:"
        }

        payload = {
            "payment_type": "qris",
            "transaction_details": {
                "order_id": str(transaction["_id"]),
                "gross_amount": math.ceil(transaction["total"])
            },
            "qris": {"acquirer": "gopay"}
        }

        response = requests.post(f"{self.url}charge", headers=headers, json=PaymentMidtransRequest(**payload).dict())
        return response.json()