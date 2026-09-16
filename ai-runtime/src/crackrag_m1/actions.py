from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)

class Citation(StrictModel):
    region_id: str
    quote: str=Field(min_length=1,max_length=800)

class Fact(Citation):
    value: str=Field(pattern=r'^-?\d+(?:\.\d+)?$')
    year: str=Field(pattern=r'^\d{4}$')
    unit: str=Field(min_length=1,max_length=24)
    entity: str=Field(min_length=1,max_length=100)

class Answer(StrictModel):
    action: Literal['answer']
    answer: str=Field(min_length=1,max_length=2000)
    citations: list[Citation]=Field(default_factory=list,max_length=5)
    facts: list[Fact]=Field(default_factory=list,max_length=6)
    unresolved: list[str]=Field(default_factory=list,max_length=5)

class Search(StrictModel):
    action: Literal['search']
    query: str=Field(min_length=1,max_length=1000)
    year: int=Field(default=0,ge=0,le=2200)
    page: int=Field(default=0,ge=0,le=10000)

class Open(StrictModel):
    action: Literal['open']
    region_ids: list[str]=Field(min_length=1,max_length=3)

class Calculate(StrictModel):
    action: Literal['calculate']
    operation: Literal['add','subtract','ratio','percent_change']
    operands: list[Fact]=Field(min_length=2,max_length=2)
    precision: int=Field(default=2,ge=0,le=6)

ACTION=TypeAdapter(Annotated[Union[Answer,Search,Open,Calculate],Field(discriminator='action')])

# Product answers contain proposals, never model-authored display prose. Go
# binds each proposal to the original question and renders only supported data.
class FinancialClaim(StrictModel):
    requirement_key: str=Field(pattern=r'^[0-9a-f]{64}$')
    region_id: str=Field(min_length=1,max_length=64)
    quote: str=Field(min_length=1,max_length=6000)
    raw_value: str=Field(min_length=1,max_length=80)
    value: str=Field(pattern=r'^-?(?:0|[1-9]\d{0,29})(?:\.\d{1,12})?$')

class FinancialAbstention(StrictModel):
    requirement_key: str=Field(pattern=r'^[0-9a-f]{64}$')
    reason_code: Literal['INSUFFICIENT_EVIDENCE','AMBIGUOUS_EVIDENCE']

class FinancialAnswer(StrictModel):
    action: Literal['answer']
    claims: list[FinancialClaim]=Field(default_factory=list,max_length=12)
    abstentions: list[FinancialAbstention]=Field(default_factory=list,max_length=6)

FINANCIAL_ACTION=TypeAdapter(Annotated[Union[FinancialAnswer,Search,Open],Field(discriminator='action')])

def validate_citation(citation,opened):
    region=opened.get(citation.region_id)
    if region is None or citation.quote not in region.text:
        raise ValueError('CITATION_NOT_OBSERVED')
    if isinstance(citation,Fact):
        values=re.findall(r'(?<![\d.])-?\d[\d,]*(?:\.\d+)?',citation.quote)
        if Decimal(citation.value) not in [Decimal(v.replace(',','')) for v in values]:
            raise ValueError('FACT_VALUE_NOT_IN_QUOTE')
    return region

def validate_answer(answer,opened):
    if not answer.citations and not answer.unresolved:
        raise ValueError('ANSWER_HAS_NO_SUPPORT_OR_ABSTENTION')
    for citation in [*answer.citations,*answer.facts]:
        validate_citation(citation,opened)

def calculate(action,opened):
    for operand in action.operands:
        validate_citation(operand,opened)
    first,second=action.operands
    if first.entity!=second.entity or first.unit!=second.unit:
        raise ValueError('CALCULATION_BASIS_MISMATCH')
    if action.operation!='percent_change' and first.year!=second.year:
        raise ValueError('CALCULATION_YEAR_MISMATCH')
    a,b=Decimal(first.value),Decimal(second.value)
    if max(a.copy_abs(),b.copy_abs())>Decimal('1e30'):
        raise ValueError('CALCULATION_MAGNITUDE_LIMIT')
    if action.operation in ('ratio','percent_change') and b==0:
        raise ValueError('DIVISION_BY_ZERO')
    try:
        with localcontext() as ctx:
            ctx.prec=80
            result={'add':lambda:a+b,'subtract':lambda:a-b,'ratio':lambda:a/b,
                    'percent_change':lambda:(a-b)/b*100}[action.operation]()
            rounded=result.quantize(Decimal(1).scaleb(-action.precision),rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError('CALCULATION_RESULT_LIMIT') from exc
    return {'formula_version':'m1-decimal-v1','operation':action.operation,
        'formula':{'add':'a+b','subtract':'a-b','ratio':'a/b','percent_change':'(a-b)/b*100'}[action.operation],
        'inputs':[v.model_dump() for v in action.operands],'precision':action.precision,
        'rounding':'ROUND_HALF_UP','result':str(rounded),
        'unit':'%' if action.operation=='percent_change' else ('ratio' if action.operation=='ratio' else first.unit),
        'semantic_basis':'model-declared basis, source quotes/numerals checked; no M2 fact publication'}
