"""Private navigation preferences; never accepts credentials or arbitrary URLs."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Favorite(Strict):
    section_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)


class SavedView(Strict):
    view_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    name: str = Field(min_length=1, max_length=80)
    view: Literal["month", "week", "day", "agenda"]
    query: str = Field(default="", max_length=120)


class NavigationState(Strict):
    revision: int = Field(ge=0)
    favorites: list[Favorite] = Field(default_factory=list, max_length=20)
    saved_views: list[SavedView] = Field(default_factory=list, max_length=10)
    onboarding_step: int = Field(default=0, ge=0, le=4)
    onboarding_completed: bool = False

    @model_validator(mode="after")
    def consistent(self):
        if len({f.section_id for f in self.favorites}) != len(self.favorites):
            raise ValueError("A section can be saved only once")
        if len({v.view_id for v in self.saved_views}) != len(self.saved_views):
            raise ValueError("Saved-view identifiers must be unique")
        if self.onboarding_completed and self.onboarding_step != 4:
            raise ValueError("Complete all onboarding steps first")
        return self


class NavigationStore:
    def __init__(self, cloud):
        self.cloud = cloud
        cloud.db.execute(
            "CREATE TABLE IF NOT EXISTS personal_navigation("
            "owner TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL)"
        )
        cloud.db.commit()

    def read(self, owner):
        row = self.cloud.db.execute(
            "SELECT payload,revision FROM personal_navigation WHERE owner=?", (owner,)
        ).fetchone()
        return (
            json.loads(row["payload"]) | {"revision": row["revision"]}
            if row
            else NavigationState(revision=0).model_dump()
        )

    def save(self, owner, body):
        with self.cloud._write_lock, self.cloud.db:
            self.cloud.db.execute("BEGIN IMMEDIATE")
            existing = self.read(owner)
            if existing["revision"] != body.revision:
                raise HTTPException(
                    409, "Preferences changed elsewhere. Refresh before saving again."
                )
            value = body.model_dump() | {"revision": body.revision + 1}
            self.cloud.db.execute(
                "INSERT INTO personal_navigation VALUES(?,?,?) "
                "ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload,revision=excluded.revision",
                (owner, json.dumps(value), value["revision"]),
            )
        return value


def install_workspace_navigation(api, cloud, operator_owner, driver_owner):
    store = NavigationStore(cloud)
    api.state.workspace_navigation = store

    def routes(dependency):
        router = APIRouter()

        @router.get("/navigation")
        def read(owner=Depends(dependency)):
            return store.read(owner["key"])

        @router.put("/navigation")
        def save(body: NavigationState, owner=Depends(dependency)):
            return store.save(owner["key"], body)

        return router

    for audience, dependency in [
        ("operator", operator_owner),
        ("driver", driver_owner),
    ]:
        api.include_router(
            routes(dependency),
            prefix=f"/api/v1/{audience}/workspace",
            tags=["Workspace navigation"],
        )
