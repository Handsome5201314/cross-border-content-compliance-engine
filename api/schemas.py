# -*- coding: utf-8 -*-
"""Pydantic 请求模型（ARCHITECTURE_V3.md §2.2 端点表请求体）。"""
from pydantic import BaseModel, Field


class AuthIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RechargeIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    amount: int = Field(ge=1, le=10_000_000)
    note: str = Field(min_length=1, max_length=200)


class GenerateIn(BaseModel):
    product_name: str = Field(min_length=1, max_length=120)
    product_name_en: str = Field(default="", max_length=120)
    form: str = Field(default="", max_length=300)
    deployment: str = Field(default="", max_length=500)
    marketing_notes: str = Field(default="", max_length=2000)
    target_markets: list[str] = Field(default_factory=list, max_length=20)
    languages: list[str] = Field(min_length=1, max_length=12)
    platforms: list[str] = Field(min_length=1, max_length=8)
    compliance_level: str = Field(default="strict", max_length=32)
    deadline_s: float = Field(default=900.0, ge=60, le=7200)
    models: dict[str, str] = Field(default_factory=dict)
