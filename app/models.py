"""登记请求的结构校验（重量为正整数克、工序投入产出平衡等）。"""
from typing import Annotated, List, Literal, Optional, Union

from pydantic import (AliasChoices, BaseModel, Field,
                      StringConstraints, model_validator)

BatchNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
TicketNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Grams = Annotated[int, Field(gt=0, description="重量，正整数克")]
Version = Annotated[int, Field(ge=0, description="磅单版号（首次登记为 1，每次换交加 1）")]


class BatchWeight(BaseModel):
    batch: BatchNo
    weight: Grams


class IncomingDoc(BaseModel):
    """进料批登记：确立批号的来源为进料。"""
    type: Literal["incoming"]
    batch: BatchNo
    weight: Grams


class ProcessDoc(BaseModel):
    """工序单：整批投入、整批产出，投入总重须等于产出总重。"""
    type: Literal["process"]
    inputs: List[BatchWeight] = Field(min_length=1)
    outputs: List[BatchWeight] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self):
        ins = [x.batch for x in self.inputs]
        outs = [x.batch for x in self.outputs]
        if len(set(ins)) != len(ins):
            raise ValueError("同一工序的投入批号重复")
        if len(set(outs)) != len(outs):
            raise ValueError("同一工序的产出批号重复")
        if set(ins) & set(outs):
            raise ValueError("同一工序的投入与产出批号不能重叠")
        sum_in = sum(x.weight for x in self.inputs)
        sum_out = sum(x.weight for x in self.outputs)
        if sum_in != sum_out:
            raise ValueError(f"工序投入总重 {sum_in} 克不等于产出总重 {sum_out} 克")
        return self


class ShipmentDoc(BaseModel):
    """整批发货登记：确立批号的去向为发货。"""
    type: Literal["shipment"]
    batch: BatchNo
    weight: Grams


class ReceiptItem(BaseModel):
    """磅单上的一行：某个整批的批号与实收净重。"""
    batch: BatchNo = Field(
        validation_alias=AliasChoices("batch", "batch_no"))
    weight: Grams = Field(
        validation_alias=AliasChoices(
            "weight", "net_weight", "received_net_grams", "received_weight"))


def _check_receipt_items(items) -> None:
    batches = [x.batch for x in items]
    if len(set(batches)) != len(batches):
        raise ValueError("同一磅单内的批号不得重复")


class ReceiptDoc(BaseModel):
    """收货磅单登记/整张换交：可早于发货单录入。

    version 为空表示新建；对已有磅单换交时须给出所见版号。
    """
    type: Literal["receipt"]
    ticket_no: TicketNo = Field(
        validation_alias=AliasChoices("ticket_no", "ticket", "ticket_number"))
    version: Optional[Version] = Field(
        default=None,
        validation_alias=AliasChoices("version", "seen_version", "expected_version"))
    items: List[ReceiptItem] = Field(
        min_length=1,
        validation_alias=AliasChoices("items", "batches", "lines", "details"))

    @model_validator(mode="after")
    def _check(self):
        _check_receipt_items(self.items)
        return self


class WeighTicketDoc(ReceiptDoc):
    """`type` 的别名写法，行为与 receipt 相同。"""
    type: Literal["weigh_ticket"]


class ReceiptRequest(BaseModel):
    """收货磅单登记/整张换交请求体。"""
    ticket_no: TicketNo = Field(
        validation_alias=AliasChoices("ticket_no", "ticket", "ticket_number"))
    version: Optional[Version] = Field(
        default=None,
        validation_alias=AliasChoices("version", "seen_version", "expected_version"))
    items: List[ReceiptItem] = Field(
        min_length=1,
        validation_alias=AliasChoices("items", "batches", "lines", "details"))

    @model_validator(mode="after")
    def _check(self):
        _check_receipt_items(self.items)
        return self


class SignRequest(BaseModel):
    """签收请求体：所见的磅单版号。"""
    version: Optional[Version] = Field(
        default=None,
        validation_alias=AliasChoices("version", "seen_version", "expected_version"))


Document = Annotated[Union[IncomingDoc, ProcessDoc, ShipmentDoc, ReceiptDoc,
                           WeighTicketDoc],
                     Field(discriminator="type")]


class RegisterRequest(BaseModel):
    """一次登记：可含多张单据，全部校验通过才落库，否则整次不生效。"""
    documents: List[Document] = Field(min_length=1)
