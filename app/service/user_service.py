from datetime import datetime
from typing import List, Tuple
from urllib.parse import urlparse
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException, UploadFile
from pymongo.results import UpdateResult
from starlette.responses import JSONResponse

from app.core.config import config
from app.core.logger import logger
from app.core.s3_client import s3_client
from app.http.middleware.auth import remove_expired_token

from app.model.user_model import User
from app.core.security import get_hashed_password, verify_password, create_access_token, create_refresh_token
from app.repository.user_repository import UserRepository
from app.schema.user_schema import RegisterUserRequest, UserResponse, LoginUserRequest, TokenResponse, GetUserRequest, \
    LogoutUserRequest, UpdateUserRequest, ChangePasswordRequest, ChangePhotoRequest, ForgetPasswordRequest, \
    AddAccountRequest, GetAccountRequest, ListAccountRequest, UpdateAccountRequest, DeleteAccountRequest, \
    WithdrawalRequest, AccountResponse, FollowRequest


class UserService:
    def __init__(self):
        self.user_repository = UserRepository()

    def register(self, request: RegisterUserRequest) -> UserResponse:
        logger.info("Register request received: {}", request.dict())
        errors = {}
        required_fields = {
            "name": "Name is required",
            "email": "Email is required",
            "password": "Password is required",
            "phone": "Phone is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning("Validation errors: {}", errors)
            raise HTTPException(status_code=400, detail=errors)

        if self.user_repository.find_by_email(request.email):
            errors["email"] = "Email already exists"
        if self.user_repository.find_by_phone(request.phone):
            errors["phone"] = "Phone already exists"

        if errors:
            logger.warning("Validation errors: {}", errors)
            raise HTTPException(status_code=400, detail=errors)

        try:
            password = get_hashed_password(request.password)
            data = {
                "name": request.name,
                "email": request.email,
                "username": request.email.split("@")[0],
                "phone": request.phone,
                "password": password,
            }
            user = User(**data)
            result = self.user_repository.create(user)
            user = self.user_repository.find_by_id(result.inserted_id)
            user['_id'] = str(user["_id"])
            user["followers"] = len(user["followers"])
            user["following"] = len(user["following"])
            logger.info("User registered successfully: {}", user)
            return UserResponse(**user)
        except Exception as e:
            logger.error("Error during user registration: {}", str(e))
            raise HTTPException(status_code=500, detail=str(e))

    def login(self, request: LoginUserRequest) -> TokenResponse:
        errors = {}
        logger.info(f"Login request received: {request.dict()}")
        required_fields = {
            "email_or_phone": "Email or Phone is required",
            "password": "Password is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        user = self.user_repository.find_email_or_phone(request.email_or_phone)
        if not user or not verify_password(request.password, user["password"]):
            errors["login"] = "Email, Phone or Password is incorrect."

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        try:
            access_token = create_access_token(user["_id"])
            refresh_token = create_refresh_token(user["_id"])

            logger.info(f"User logged in successfully: {user}")
            return TokenResponse(access_token=access_token, refresh_token=refresh_token, token_type="bearer")
        except Exception as e:
            logger.error(f"Error during user login: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def get(self, request: GetUserRequest) -> UserResponse:
        logger.info(f"Get user request received: {request.dict()}")
        try:
            user = self.user_repository.find_by_id(ObjectId(request.id), exclude=["password", "accounts"])
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            user["photo"] = s3_client.get_object(config.aws_bucket, urlparse(user["photo"]).path.lstrip("/")) if user.get("photo") else None
            user['_id'] = str(user["_id"])
            user["followers"] = len(user["followers"])
            user["following"] = len(user["following"])
            logger.info(f"User found: {user}")
            return UserResponse(**user)
        except Exception as e:
            logger.error(f"Error during get user: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def logout(self, request: LogoutUserRequest) -> bool:
        logger.info(f"Logout user request received: {request.dict()}")
        try:
            user = self.user_repository.find_by_id(ObjectId(request.id))
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            if not request.access_token:
                raise HTTPException(status_code=400, detail="Access token is required")
            if not request.refresh_token:
                raise HTTPException(status_code=400, detail="Refresh token is required")
            remove_expired_token(request.refresh_token, config.jwt_refresh_key)
            remove_expired_token(request.access_token, config.jwt_secret_key)
            logger.info(f"User logged out successfully: {request.id}")
            return True
        except Exception as e:
            logger.error(f"Error during logout user: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def update(self, request: UpdateUserRequest) -> UserResponse:
        errors = {}
        logger.info(f"Update user request received: {request.dict()}")
        required_fields = {
            "id": "ID is required",
        }

        if not getattr(request, "id"):
            errors["id"] = required_fields["id"]

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)
        try:
            user = self.user_repository.find_by_id(ObjectId(request.id))
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            if request.email is not None:
                if self.user_repository.find_by_email(request.email) and request.email != user["email"]:
                    errors["email"] = "Email already exists"
                else:
                    user["email"] = request.email

            if request.phone is not None and request.phone != user["phone"]:
                if self.user_repository.find_by_phone(request.phone):
                    errors["phone"] = "Phone already exists"
                else:
                    user["phone"] = request.phone

            if request.username is not None and request.username != user["username"]:
                if self.user_repository.find_by_username(request.username):
                    errors["username"] = "Username already exists"
                else:
                    user["username"] = request.username

            if errors:
                logger.warning(f"Validation errors: {errors}")
                raise HTTPException(status_code=400, detail=errors)

            user = User(**user)

            update_result: UpdateResult = self.user_repository.update(user)
            if update_result.modified_count == 1 or update_result.upserted_id:
                logger.info(f"User updated successfully: {request.dict()}")
                updated_user = self.user_repository.find_by_id(ObjectId(request.id))
                updated_user['_id'] = str(updated_user["_id"])
                updated_user["followers"] = len(updated_user["followers"])
                updated_user["following"] = len(updated_user["following"])
                return UserResponse(**updated_user)
        except Exception as e:
            logger.error(f"Error during update user: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def change_password(self, request: ChangePasswordRequest) -> bool:
        logger.info(f"Change password request received: {request.dict()}")
        try:
            user = self.user_repository.find_by_id(ObjectId(request.id))
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            if not request.new_password == request.confirm_password:
                raise HTTPException(status_code=400, detail="Password and confirm password do not match.")

            if not verify_password(request.old_password, user["password"]):
                raise HTTPException(status_code=400, detail="Old password is incorrect.")

            password = get_hashed_password(request.new_password)
            self.user_repository.change_password(ObjectId(request.id), password)
            logger.info(f"Password changed successfully: {request.id}")
            return True

        except HTTPException as e:
            logger.error(f"Error during change password: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"Error during change password: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def change_profile(self, request: ChangePhotoRequest, file: UploadFile) -> UserResponse:
        errors = {}
        logger.info(f"Change photo request received: {request.dict()}")
        required_fields = {
            "id": "ID is required",
            "photo": "Photo is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        try:
            user = self.user_repository.find_by_id(ObjectId(request.id))
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            file_extension = file.filename.split(".")[-1]
            path = f"profile/{uuid4()}_{user['_id']}.{file_extension}"

            # Upload the file to S3
            file.file.seek(0)  # Ensure the file pointer is at the beginning
            s3_client.upload_file(file.file, config.aws_bucket, path)
            url = f"{config.aws_url}{path}"
            user["photo"] = url
            data = User(**user)
            update_result: UpdateResult = self.user_repository.update(data)
            if update_result.modified_count == 1 or update_result.upserted_id:
                logger.info(f"Photo changed successfully: {url}")
                updated_user = self.user_repository.find_by_id(ObjectId(request.id))
                updated_user['photo'] = s3_client.get_object(config.aws_bucket,
                                                             urlparse(updated_user["photo"]).path.lstrip("/"))
                updated_user['_id'] = str(updated_user["_id"])
                updated_user["followers"] = len(updated_user["followers"])
                updated_user["following"] = len(updated_user["following"])
                return UserResponse(**updated_user)
            else:
                logger.error(f"Failed to change photo: {update_result.raw_result}")
                raise HTTPException(status_code=500, detail="Failed to change photo")
        except Exception as e:
            logger.error(f"Error during change photo: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def forget_password(self, request: ForgetPasswordRequest):
        pass

    def add_account(self, request: AddAccountRequest) -> AccountResponse:
        logger.info(f"Add account request received: {request.dict()}")
        errors = {}
        required_fields = {
            "id": "ID is required",
            "bank": "Bank is required",
            "name": "Name is required",
            "number": "Number is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        user = self.user_repository.find_by_id(ObjectId(request.id))
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        account = self.user_repository.find_account_by_number(ObjectId(request.id), request.number, request.bank)
        if account:
            raise HTTPException(status_code=400, detail="Account already exists")

        try:
            data = request.dict(exclude={"id"})
            data["_id"] = ObjectId()
            data["created_at"] = datetime.utcnow()
            data["updated_at"] = datetime.utcnow()
            data["deleted_at"] = None
            update_result: UpdateResult = self.user_repository.add_account(ObjectId(request.id), data)
            if update_result.upserted_id or update_result.modified_count == 1:
                logger.info(f"Account added successfully: {data}")
                data["_id"] = str(data["_id"])
                return AccountResponse(**data)
            else:
                raise HTTPException(status_code=500, detail="Failed to add account")
        except Exception as e:
            logger.error(f"Error during add account: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def get_account(self, request: GetAccountRequest) -> AccountResponse:
        logger.info(f"Get account request received: {request.dict()}")
        errors = {}
        required_fields = {
            "id": "ID is required",
            "account_id": "Account ID is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        try:
            account = self.user_repository.find_account_by_id(ObjectId(request.id), ObjectId(request.account_id))
            if not account or not account.get("accounts"):
                raise HTTPException(status_code=404, detail="Account not found")

            account = account["accounts"][0]
            account["_id"] = str(account["_id"])
            logger.info(f"Account found: {account}")
            return AccountResponse(**account)
        except Exception as e:
            logger.error(f"Error during get account: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def list_account(self, request: ListAccountRequest) -> Tuple[List[AccountResponse], int]:
        logger.info(f"List account request received: {request.dict()}")
        try:
            accounts, total = self.user_repository.list(request)
            logger.info(f"Accounts found: {accounts}")
            for account in accounts:
                account["_id"] = str(account["_id"])
            logger.info(f"Accounts found: {accounts}")
            return [AccountResponse(**account) for account in accounts], total
        except Exception as e:
            logger.error(f"Error during list account: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def update_account(self, request: UpdateAccountRequest) -> AccountResponse:
        logger.info(f"Update account request received: {request.dict()}")
        errors = {}
        required_fields = {
            "id": "ID is required",
            "account_id": "Account ID is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        account = self.user_repository.find_account_by_id(ObjectId(request.id), ObjectId(request.account_id))
        if not account or not account.get("accounts"):
            raise HTTPException(status_code=404, detail="Account not found")

        if request.bank is not None:
            account["accounts"][0]["bank"] = request.bank
        if request.name is not None:
            account["accounts"][0]["name"] = request.name
        if request.number is not None:
            account["accounts"][0]["number"] = request.number

        try:
            if request.bank != account["accounts"][0]["bank"] or request.number != account["accounts"][0]["number"]:
                check_account = self.user_repository.find_account_by_number(ObjectId(request.id), request.number,
                                                                            request.bank)
                if check_account:
                    raise HTTPException(status_code=400, detail="Account already exists")

            account["accounts"][0]["updated_at"] = datetime.utcnow()
            update_result: UpdateResult = self.user_repository.update_account(ObjectId(request.id), ObjectId(request.account_id), account["accounts"][0])
            if update_result.modified_count == 1:
                logger.info(f"Account updated successfully: {account}")
                updated_account = self.user_repository.find_account_by_id(ObjectId(request.id),
                                                                          ObjectId(request.account_id))
                updated_account = updated_account["accounts"][0]
                updated_account["_id"] = str(updated_account["_id"])
                logger.info(f"Updated account: {updated_account}")
                return AccountResponse(**updated_account)
            else:
                raise HTTPException(status_code=500, detail="Failed to update account")
        except Exception as e:
            logger.error(f"Error during update account: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def delete_account(self, request: DeleteAccountRequest) -> bool:
        logger.info(f"Delete account request received: {request.dict()}")
        errors = {}
        required_fields = {
            "id": "ID is required",
            "account_id": "Account ID is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        account = self.user_repository.find_account_by_id(ObjectId(request.id), ObjectId(request.account_id))
        if not account or not account.get("accounts"):
            raise HTTPException(status_code=404, detail="Account not found")

        try:
            update_result: UpdateResult = self.user_repository.delete_account(ObjectId(request.id), ObjectId(request.account_id))
            if update_result.modified_count == 1:
                logger.info(f"Account deleted successfully: {request.account_id}")
                return True
            else:
                raise HTTPException(status_code=500, detail="Failed to delete account")
        except Exception as e:
            logger.error(f"Error during delete account: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def withdrawal(self, request: WithdrawalRequest) -> UserResponse:
        logger.info(f"Withdrawal request received: {request.dict()}")
        errors = {}
        required_fields = {
            "id": "ID is required",
            "amount": "Amount is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        user = self.user_repository.find_by_id(ObjectId(request.id))
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            if user["balance"] < request.amount or request.amount <= 0:
                raise HTTPException(status_code=400, detail="Balance is not enough")

            user["balance"] -= request.amount
            data = User(**user)
            update_result: UpdateResult = self.user_repository.update(data)
            if update_result.modified_count == 1 or update_result.upserted_id:
                logger.info(f"Withdrawal successful: {request.amount}")
                updated_user = self.user_repository.find_by_id(ObjectId(request.id))
                updated_user['_id'] = str(updated_user["_id"])
                updated_user["followers"] = len(updated_user["followers"])
                updated_user["following"] = len(updated_user["following"])
                return UserResponse(**updated_user)
            else:
                raise HTTPException(status_code=500, detail="Failed to withdrawal")
        except Exception as e:
            logger.error(f"Error during withdrawal: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def follow(self, request: FollowRequest) -> bool:
        logger.info(f"Follow request received: {request.dict()}")
        errors = {}
        required_fields = {
            "id": "ID is required",
            "target_id": "Target ID is required"
        }

        for field, error_message in required_fields.items():
            if not getattr(request, field):
                errors[field] = error_message

        if errors:
            logger.warning(f"Validation errors: {errors}")
            raise HTTPException(status_code=400, detail=errors)

        user = self.user_repository.find_by_id(ObjectId(request.id), exclude=["password", "accounts"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        target = self.user_repository.find_by_id(ObjectId(request.target_id), exclude=["password", "accounts"])
        if not target:
            raise HTTPException(status_code=404, detail="Target not found")

        try:
            if request.id == request.target_id:
                raise HTTPException(status_code=400, detail="Cannot follow yourself")

            if request.follow:
                if ObjectId(request.target_id) in user["following"]:
                    raise HTTPException(status_code=400, detail="Already following")
                self.user_repository.add_following(ObjectId(request.id), ObjectId(request.target_id))
            else:
                if ObjectId(request.target_id) not in user["following"]:
                    raise HTTPException(status_code=400, detail="Not following")
                self.user_repository.remove_following(ObjectId(request.id), ObjectId(request.target_id))

            logger.info(f"Follow successful: {request.target_id}")
            return True
        except Exception as e:
            logger.error(f"Error during follow: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))