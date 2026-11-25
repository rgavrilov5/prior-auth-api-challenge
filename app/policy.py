from app.auth import Actor
from app.errors import reject


def visibility(actor: Actor) -> tuple[str, tuple]:
    # Each caller group owns an explicit policy and an independent HTTP surface.
    match actor.role:
        case "office":
            return "r.office_id = %s", (actor.id,)
        case "member":
            return "r.member_id IN (SELECT id FROM members WHERE user_id = %s)", (actor.id,)
        case "reviewer":
            return (
                """r.reviewer_id = %s AND NOT EXISTS (
                SELECT 1 FROM reviewer_patients rp
                WHERE rp.reviewer_id = %s AND rp.member_id = r.member_id)""",
                (
                    actor.id,
                    actor.id,
                ),
            )
        case "admin":
            return "TRUE", ()
        case _:
            reject(403, "role", "forbidden", "Unknown caller role")


def check_conflict(db, reviewer_id: str, member_id: str) -> None:
    conflict = db.execute(
        "SELECT 1 FROM reviewer_patients WHERE reviewer_id = %s AND member_id = %s",
        (reviewer_id, member_id),
    ).fetchone()
    if conflict:
        reject(403, "reviewer_id", "treating_patient", "Reviewer treats this patient")
