"""登记请求的结构校验（重量为正整数克、工序投入产出平衡等）。"""
from typing import Annotated, List, Literal, Union

from pydantic import BaseModel, Field, StringConstraints, model_validator

BatchNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Grams = Annotated[int, Field(gt=0, description="重量，正整数克")]


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


Document = Annotated[Union[IncomingDoc, ProcessDoc, ShipmentDoc],
                     Field(discriminator="type")]


class RegisterRequest(BaseModel):
    """一次登记：可含多张单据，全部校验通过才落库，否则整次不生效。"""
    documents: List[Document] = Field(min_length=1)
