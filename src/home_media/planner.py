"""Deterministic action planner — no LLM."""

from __future__ import annotations

from home_media.errors import SafetyBlockedError, UnsupportedError
from home_media.models import ActionPlan, PlanStep
from home_media.registry import RoomRegistry


class Planner:
    def __init__(self, registry: RoomRegistry) -> None:
        self.registry = registry

    def plan_power(self, room_name: str, state: str, *, dry_run: bool = False) -> ActionPlan:
        room = self.registry.resolve_room(room_name)
        targets = self.registry.room_targets(room.key)
        steps: list[PlanStep] = []
        warnings: list[str] = []

        if state == "off":
            # Power-off is high-risk; caller must also pass explicit confirmation flag.
            warnings.append(
                "Power off requires explicit same-turn confirmation at the service layer"
            )

        if targets["apple_tv"] and room.preferred_power_path in {"apple_tv_cec", "both"}:
            steps.append(
                PlanStep(
                    action="set_power",
                    target_device_id=targets["apple_tv"].id,
                    adapter=targets["apple_tv"].adapter,
                    params={"state": state},
                    dry_run=dry_run,
                )
            )
        if targets["physical_tv"] and room.preferred_power_path in {"physical_tv", "both"}:
            steps.append(
                PlanStep(
                    action="set_power",
                    target_device_id=targets["physical_tv"].id,
                    adapter=targets["physical_tv"].adapter,
                    params={"state": state},
                    dry_run=dry_run,
                )
            )
        if not steps:
            raise UnsupportedError("power", reason=f"No power target configured for {room.key}")

        if state == "on" and targets["apple_tv"] and not targets["physical_tv"]:
            warnings.append(
                "Physical TV power cannot be verified directly; CEC wake is unverified"
            )

        return ActionPlan(
            intent=f"power.{state}",
            room_key=room.key,
            steps=steps,
            warnings=warnings,
        )

    def plan_volume_set(
        self,
        room_name: str,
        level: int,
        *,
        override_ceiling: bool = False,
        dry_run: bool = False,
    ) -> ActionPlan:
        room = self.registry.resolve_room(room_name)
        if not 0 <= level <= 100:
            raise SafetyBlockedError("Volume must be 0-100", reason="range")
        ceiling = self.registry.config.volume_ceiling
        if (
            level > ceiling
            and self.registry.config.volume_ceiling_override_required
            and not override_ceiling
        ):
            raise SafetyBlockedError(
                f"Volume {level} exceeds ceiling {ceiling}; pass override_ceiling=true",
                reason="volume_ceiling",
            )

        audio = self.registry.room_targets(room.key)["audio"]
        if room.preferred_volume_target == "sonos" and audio is not None:
            return ActionPlan(
                intent="volume.set",
                room_key=room.key,
                steps=[
                    PlanStep(
                        action="set_volume",
                        target_device_id=audio.id,
                        adapter=audio.adapter,
                        params={"level": level},
                        dry_run=dry_run,
                    )
                ],
            )
        if room.preferred_volume_target == "apple_tv_cec":
            raise UnsupportedError(
                "volume.set_absolute",
                reason="Room prefers CEC relative volume; refuse exact set without absolute route",
            )
        raise UnsupportedError("volume.set_absolute", reason="No absolute volume target configured")

    def plan_open_app(self, room_name: str, app: str, *, dry_run: bool = False) -> ActionPlan:
        room = self.registry.resolve_room(room_name)
        apple = self.registry.room_targets(room.key)["apple_tv"]
        if apple is None:
            raise UnsupportedError("apps.launch", reason="No Apple TV in room")
        app_id = self.registry.config.app_aliases.get(app.lower(), app)
        return ActionPlan(
            intent="app.open",
            room_key=room.key,
            steps=[
                PlanStep(
                    action="open_app",
                    target_device_id=apple.id,
                    adapter=apple.adapter,
                    params={"app_id": app_id, "requested": app},
                    dry_run=dry_run,
                )
            ],
        )

    def plan_open_content(
        self,
        room_name: str,
        *,
        url: str | None = None,
        alias: str | None = None,
        expected_app: str | None = None,
        expected_title: str | None = None,
        dry_run: bool = False,
    ) -> ActionPlan:
        room = self.registry.resolve_room(room_name)
        apple = self.registry.room_targets(room.key)["apple_tv"]
        if apple is None:
            raise UnsupportedError("content.open", reason="No Apple TV in room")
        if alias and not url:
            url = self.registry.config.content_aliases.get(alias)
            if not url:
                raise UnsupportedError("content.alias", reason=f"Unknown alias '{alias}'")
        if not url:
            raise UnsupportedError("content.open", reason="url or alias required")
        return ActionPlan(
            intent="content.open",
            room_key=room.key,
            steps=[
                PlanStep(
                    action="open_url",
                    target_device_id=apple.id,
                    adapter=apple.adapter,
                    params={
                        "url": url,
                        "alias": alias,
                        "expected_app": expected_app,
                        "expected_title": expected_title,
                    },
                    dry_run=dry_run,
                )
            ],
        )

    def plan_transport(self, room_name: str, action: str, *, dry_run: bool = False) -> ActionPlan:
        room = self.registry.resolve_room(room_name)
        apple = self.registry.room_targets(room.key)["apple_tv"]
        if apple is None:
            raise UnsupportedError("transport", reason="No Apple TV in room")
        return ActionPlan(
            intent=f"transport.{action}",
            room_key=room.key,
            steps=[
                PlanStep(
                    action="control_transport",
                    target_device_id=apple.id,
                    adapter=apple.adapter,
                    params={"action": action},
                    dry_run=dry_run,
                )
            ],
        )

    def plan_tv_input(self, room_name: str, source: str, *, dry_run: bool = False) -> ActionPlan:
        room = self.registry.resolve_room(room_name)
        tv = self.registry.room_targets(room.key)["physical_tv"]
        if tv is None:
            raise UnsupportedError("tv.input", reason="No physical TV in room")
        return ActionPlan(
            intent="tv.input",
            room_key=room.key,
            steps=[
                PlanStep(
                    action="set_input",
                    target_device_id=tv.id,
                    adapter=tv.adapter,
                    params={"source": source},
                    dry_run=dry_run,
                )
            ],
        )

    def plan_watch_scene(
        self,
        room_name: str,
        *,
        service: str | None = None,
        url: str | None = None,
        volume: int | None = None,
        override_ceiling: bool = False,
        dry_run: bool = False,
    ) -> ActionPlan:
        room = self.registry.resolve_room(room_name)
        steps: list[PlanStep] = []
        warnings: list[str] = []
        power_plan = self.plan_power(room_name, "on", dry_run=dry_run)
        steps.extend(power_plan.steps)
        warnings.extend(power_plan.warnings)

        if url:
            steps.extend(self.plan_open_content(room_name, url=url, dry_run=dry_run).steps)
        elif service:
            steps.extend(self.plan_open_app(room_name, service, dry_run=dry_run).steps)
        else:
            warnings.append("Watch scene has no content/app target")

        if volume is not None:
            steps.extend(
                self.plan_volume_set(
                    room_name,
                    volume,
                    override_ceiling=override_ceiling,
                    dry_run=dry_run,
                ).steps
            )

        return ActionPlan(
            intent="scene.watch",
            room_key=room.key,
            steps=steps,
            warnings=warnings,
        )
