from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from estafeta.domain.access import Role

Password = Annotated[str, StringConstraints(min_length=12, max_length=1024)]
TotpCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{6}$")]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=1024)


class LoginResponse(BaseModel):
    authenticated: bool
    totp_required: bool
    totp_setup_required: bool
    csrf_token: str


class InvitationAcceptance(BaseModel):
    token: str = Field(min_length=32, max_length=200)
    password: Password


class TotpCodeRequest(BaseModel):
    code: TotpCode


class TotpSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    email: EmailStr
    role: Role
    is_active: bool


class UserCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    role: Role
    position_id: UUID


class InvitationResponse(BaseModel):
    user: AuthenticatedUser
    invitation_url: str


class CsvImportResponse(BaseModel):
    invitations: list[InvitationResponse]
