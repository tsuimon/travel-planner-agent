"""Validated capabilities exposed to the conversational decision maker."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from src.domain import ItineraryDraft, Model


class Reply(Model):
    action: Literal["reply"]
    message: str = Field(min_length=1, max_length=3000)
    clarification: bool = False


class PlanTrip(Model):
    action: Literal["plan"]
    mode: Literal["new", "update", "preview"]
    patch: dict


class ProbeRoute(Model):
    action: Literal["probe"]
    origin: str = Field(min_length=1, max_length=128)
    destination: str = Field(min_length=1, max_length=128)
    at: str
    line: str | None = Field(None, max_length=80)


class Lookup(Model):
    action: Literal["lookup"]
    tool: Literal["amap_places", "weather_query", "rag_query", "web_search"]
    arguments: dict


class Remember(Model):
    action: Literal["remember"]
    preferences: dict


class DraftAudit(Model):
    patch: dict = Field(default_factory=dict)
    question: str | None = Field(None, max_length=500)


ACTION = TypeAdapter(
    Annotated[Reply | PlanTrip | ProbeRoute | Lookup | Remember, Field(discriminator="action")]
)


def patch_draft(previous: ItineraryDraft, patch: dict) -> ItineraryDraft:
    """Omitted fields survive edits; explicit nulls clear optional fields.

    A stop list is an ordered replacement; the model must retain unchanged stops.
    Unknown keys and all resulting constraints are validated by the domain model.
    """
    value = previous.model_dump(mode="json")
    for key, item in patch.items():
        if key == "preferences" and isinstance(item, dict):
            value[key] = {**value[key], **item}
        else:
            value[key] = item
    return ItineraryDraft.model_validate(value)
