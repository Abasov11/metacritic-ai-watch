"""LLM-as-judge check that the review summaries stay close to the reviews.

Every bullet of every summary is handed to a second model together with the reviews the
summary was built from, and the judge says whether some review actually supports it.
The judge is a third vendor (see `EVAL_JUDGE_MODEL`) so no model ever grades its own
output. All bullets of one summary travel in a single call.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal, init_db
from app.llm import MAX_REVIEW_CHARS, MAX_REVIEWS, chat_json
from app.models import Game, LlmCall, Review, Summary
from app.similar import _tokenize

log = logging.getLogger(__name__)

JUDGE_SCHEMA = (
    '{"verdicts": [{"index": 1, "supported": true, '
    '"evidence": "цитата из отзыва ≤200 символов", "note": "пояснение"}]}'
)
MAX_EVIDENCE_CHARS = 200

#: Fabrications fed to the judge alongside the real work. A judge that rubber-stamps
#: everything would report 100% support and mean nothing, so every run proves it can
#: still say no. These must not be true of any game.
CONTROL_CLAIMS = [
    ("likes", "Игра поддерживает кооператив на восемь игроков по сети"),
    ("likes", "Отдельно хвалят режим гонок на верблюдах"),
    ("dislikes", "Ругают обязательную подписку за 900 рублей в месяц"),
]


@dataclass
class Verdict:
    slug: str
    title: str
    kind: str  # critic | user
    sign: str  # likes | dislikes
    claim: str
    supported: bool
    evidence: str
    note: str
    nearest: str = ""  # closest review, filled in for unsupported claims

    @property
    def sign_label(self) -> str:
        return "нравится" if self.sign == "likes" else "не нравится"


# ------------------------------------------------------------------------- judging


def build_prompt(title: str, kind: str, claims: list[tuple[str, str]], reviews: list[str]) -> str:
    who = "критиков" if kind == "critic" else "игроков"
    numbered_claims = "\n".join(
        f"{i}. [{'плюс' if sign == 'likes' else 'минус'}] {text}"
        for i, (sign, text) in enumerate(claims, start=1)
    )
    numbered_reviews = "\n\n".join(
        f"[Отзыв {i}] {text.strip()[:MAX_REVIEW_CHARS]}"
        for i, text in enumerate(reviews[:MAX_REVIEWS], start=1)
    )
    return (
        f"Игра: {title}\n\n"
        f"Ниже утверждения из автоматического резюме отзывов {who}, а затем сами "
        f"отзывы.\n\nУТВЕРЖДЕНИЯ:\n{numbered_claims}\n\n"
        f"ОТЗЫВЫ:\n{numbered_reviews}\n\n"
        "Для каждого утверждения реши, подтверждается ли оно хотя бы одним из отзывов "
        "выше. Подтверждением считается отзыв, где та же мысль высказана прямо или "
        "очевидным перефразом. Не засчитывай общее правдоподобие, знание об игре извне "
        "и догадки. Если подтверждения нет — supported=false.\n"
        "Верни по одному вердикту на каждое утверждение, с его номером в index. "
        "В evidence — дословная цитата из подтверждающего отзыва не длиннее 200 "
        "символов, при supported=false оставь evidence пустым. "
        "В note — одно короткое предложение по-русски, почему так."
    )


def parse_verdicts(payload: dict, claims: list[tuple[str, str]]) -> list[dict]:
    """Line the judge's answer up with the claims, one entry per claim.

    The judge may drop, duplicate or renumber entries; a claim without a usable verdict
    counts as unsupported rather than silently disappearing from the metrics.
    """
    by_index: dict[int, dict] = {}
    raw = payload.get("verdicts")
    if isinstance(raw, list):
        for position, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                continue
            # Anything that is not a whole number falls back to the position in the
            # list; `int()` would quietly turn 3.7 into a verdict for claim 3.
            raw_index = item.get("index", position)
            if isinstance(raw_index, int) and not isinstance(raw_index, bool):
                index = raw_index
            elif isinstance(raw_index, str) and raw_index.strip().isdigit():
                index = int(raw_index.strip())
            else:
                index = position
            by_index.setdefault(index, item)

    results = []
    for number in range(1, len(claims) + 1):
        item = by_index.get(number)
        if item is None:
            results.append({"supported": False, "evidence": "", "note": "судья не вернул вердикт"})
            continue
        results.append(
            {
                "supported": bool(item.get("supported")),
                "evidence": str(item.get("evidence") or "")[:MAX_EVIDENCE_CHARS],
                "note": str(item.get("note") or ""),
            }
        )
    return results


#: Russian inflects heavily ("парирование" vs "парированием"), so words are compared
#: by their opening letters rather than whole. Crude, but this only picks a quote to
#: show next to an unsupported claim.
STEM_CHARS = 5


def _stems(text: str) -> set[str]:
    return {word[:STEM_CHARS] for word in _tokenize(text)}


def nearest_review(claim: str, reviews: list[str]) -> str:
    """Review with the most words in common — context for an unsupported claim."""
    wanted = _stems(claim)
    if not wanted or not reviews:
        return ""
    best = max(reviews, key=lambda review: len(wanted & _stems(review)))
    if not wanted & _stems(best):
        return ""
    return best.strip()[:MAX_EVIDENCE_CHARS]


def judge_summary(
    title: str, kind: str, claims: list[tuple[str, str]], reviews: list[str], game_id: int | None
) -> list[dict]:
    payload = chat_json(
        build_prompt(title, kind, claims, reviews),
        JUDGE_SCHEMA,
        purpose="eval",
        game_id=game_id,
        models=[settings.eval_judge_model],
    )
    return parse_verdicts(payload, claims)


# ------------------------------------------------------------------------ the run


def collect(session, limit: int | None = None, kind: str | None = None) -> list[Verdict]:
    """Judge every stored summary that an LLM actually wrote."""
    statement = select(Summary).where(Summary.model.is_not(None)).order_by(Summary.id)
    if kind:
        statement = statement.where(Summary.kind == kind)
    summaries = list(session.scalars(statement).all())
    if limit:
        summaries = summaries[:limit]

    verdicts: list[Verdict] = []
    for summary in summaries:
        game = session.get(Game, summary.game_id)
        claims = [("likes", c) for c in summary.likes] + [("dislikes", c) for c in summary.dislikes]
        if not claims:
            continue
        reviews = list(
            session.scalars(
                select(Review.text)
                .where(Review.game_id == summary.game_id, Review.kind == summary.kind)
                .limit(MAX_REVIEWS)
            ).all()
        )
        if not reviews:
            log.warning("no reviews left for %s/%s, skipping", game.slug, summary.kind)
            continue

        log.info("judging %s/%s (%d claims)", game.slug, summary.kind, len(claims))
        try:
            judged = judge_summary(game.title, summary.kind, claims, reviews, game.id)
        except Exception as exc:
            log.warning("judge failed for %s/%s: %s", game.slug, summary.kind, exc)
            judged = [
                {"supported": False, "evidence": "", "note": f"судья недоступен: {exc}"}
            ] * len(claims)

        for (sign, claim), result in zip(claims, judged, strict=True):
            verdict = Verdict(
                slug=game.slug,
                title=game.title,
                kind=summary.kind,
                sign=sign,
                claim=claim,
                supported=result["supported"],
                evidence=result["evidence"],
                note=result["note"],
            )
            if not verdict.supported:
                verdict.nearest = nearest_review(claim, reviews)
            verdicts.append(verdict)
    return verdicts


def control_check(session) -> list[Verdict]:
    """Judge invented claims against real reviews; all of them should be rejected."""
    summary = session.scalars(
        select(Summary).where(Summary.model.is_not(None)).order_by(Summary.id)
    ).first()
    if summary is None:
        return []
    game = session.get(Game, summary.game_id)
    reviews = list(
        session.scalars(
            select(Review.text)
            .where(Review.game_id == summary.game_id, Review.kind == summary.kind)
            .limit(MAX_REVIEWS)
        ).all()
    )
    if not reviews:
        return []

    log.info("control check against %s/%s", game.slug, summary.kind)
    try:
        judged = judge_summary(game.title, summary.kind, CONTROL_CLAIMS, reviews, game.id)
    except Exception as exc:
        log.warning("control check failed: %s", exc)
        return []
    return [
        Verdict(
            slug=game.slug,
            title=game.title,
            kind=summary.kind,
            sign=sign,
            claim=claim,
            supported=result["supported"],
            evidence=result["evidence"],
            note=result["note"],
        )
        for (sign, claim), result in zip(CONTROL_CLAIMS, judged, strict=True)
    ]


def metrics(verdicts: list[Verdict]) -> dict:
    """Supported share overall, per review kind and per sign."""

    def share(subset: list[Verdict]) -> tuple[int, int, float | None]:
        ok = sum(1 for v in subset if v.supported)
        return ok, len(subset), (round(100 * ok / len(subset), 1) if subset else None)

    return {
        "total": share(verdicts),
        "critic": share([v for v in verdicts if v.kind == "critic"]),
        "user": share([v for v in verdicts if v.kind == "user"]),
        "likes": share([v for v in verdicts if v.sign == "likes"]),
        "dislikes": share([v for v in verdicts if v.sign == "dislikes"]),
        "games": len({v.slug for v in verdicts}),
        "summaries": len({(v.slug, v.kind) for v in verdicts}),
    }


# --------------------------------------------------------------------- the report


def _row(label: str, share: tuple[int, int, float | None]) -> str:
    ok, total, percent = share
    return f"| {label} | {ok} / {total} | {'—' if percent is None else f'{percent}%'} |"


def render_report(
    verdicts: list[Verdict],
    stats: dict,
    cost: float,
    calls: int,
    control: list[Verdict] | None = None,
) -> str:
    authors = sorted({settings.openrouter_model})
    supported = [v for v in verdicts if v.supported and v.evidence]
    unsupported = [v for v in verdicts if not v.supported]

    lines = [
        "# Проверка резюме отзывов (LLM-as-judge)",
        "",
        f"Дата прогона: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}.",
        f"Резюме писала модель: `{', '.join(authors)}`. Судья: `{settings.eval_judge_model}`.",
        f"Проверено {stats['summaries']} резюме по {stats['games']} играм, "
        f"{stats['total'][1]} утверждений, {calls} вызовов судьи, ${cost:.6f} "
        "(включая контрольный вызов).",
        "",
        "## Методика",
        "",
        "Каждый пункт `likes`/`dislikes` из резюме отдаётся судье вместе с теми же "
        "отзывами, по которым резюме и строилось (до 40 штук, каждый обрезан до 700 "
        "символов). Судья отвечает по одному вердикту на пункт.",
        "",
        "**Подтверждением считается** отзыв, где та же мысль высказана прямо или "
        "очевидным перефразом. Правдоподобность сама по себе, знание об игре извне и "
        "догадки не засчитываются.",
        "",
        "Судья — модель другого вендора, она не участвует в написании резюме ни как "
        "основная, ни как резервная, поэтому никто не проверяет сам себя.",
        "",
        "**Ограничения.** Судья — та же технология, что и автор резюме, и ошибается "
        "в обе стороны: может не увидеть подтверждение за перефразом или, наоборот, "
        "засчитать слишком вольную связь. Выборка маленькая, доверительных интервалов "
        "тут нет. Метрика показывает укоренённость в тексте отзывов, а не то, "
        "справедливо ли утверждение по отношению к самой игре.",
        "",
        "## Контроль судьи",
        "",
        "Чтобы стопроцентная доля не оказалась следствием сговорчивого судьи, в тот же "
        "прогон подмешиваются заведомо выдуманные утверждения по настоящим отзывам. "
        "Исправный судья обязан отвергнуть их все.",
        "",
    ]
    if control:
        rejected = sum(1 for v in control if not v.supported)
        lines += [f"Отвергнуто {rejected} из {len(control)} выдумок.", ""]
        for verdict in control:
            mark = "отверг" if not verdict.supported else "**ПОДТВЕРДИЛ — судье нет доверия**"
            lines += [f"- {mark}: «{verdict.claim}» — {verdict.note or '—'}"]
        lines += [""]
        if rejected < len(control):
            lines += [
                "> **Внимание.** Судья засчитал выдумку, поэтому доли выше завышены и "
                "их нельзя принимать на веру.",
                "",
            ]
    else:
        lines += ["Контроль в этом прогоне не выполнялся.", ""]

    lines += [
        "## Метрики",
        "",
        "| Срез | Подтверждено | Доля |",
        "|---|---|---|",
        _row("**Всего**", stats["total"]),
        _row("Отзывы критиков", stats["critic"]),
        _row("Отзывы игроков", stats["user"]),
        _row("Пункты «нравится»", stats["likes"]),
        _row("Пункты «не нравится»", stats["dislikes"]),
        "",
    ]

    lines += ["## Примеры подтверждённых утверждений", ""]
    if supported:
        for verdict in supported[:10]:
            lines += [
                f"**{verdict.title}** · {verdict.kind} · {verdict.sign_label}",
                f"> Утверждение: {verdict.claim}",
                ">",
                f"> Цитата из отзыва: «{verdict.evidence}»",
                "",
            ]
    else:
        lines += ["Подтверждённых утверждений с цитатой не нашлось.", ""]

    lines += [f"## Неподтверждённые утверждения — все {len(unsupported)}", ""]
    if not unsupported:
        lines += ["Судья подтвердил каждое утверждение.", ""]
    for verdict in unsupported:
        lines += [
            f"**{verdict.title}** · {verdict.kind} · {verdict.sign_label}",
            f"> Утверждение: {verdict.claim}",
            ">",
            f"> Судья: {verdict.note or '—'}",
        ]
        if verdict.nearest:
            lines += [">", f"> Ближайший по словам отзыв: «{verdict.nearest}»"]
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade stored summaries against reviews.")
    parser.add_argument("--limit", type=int, default=None, help="max summaries to judge")
    parser.add_argument("--kind", choices=["critic", "user"], default=None)
    parser.add_argument("--report", type=Path, default=None, help="write markdown here")
    parser.add_argument(
        "--no-control", action="store_true", help="skip the fabricated-claims check"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    init_db()

    with SessionLocal() as session:
        before = session.scalar(select(func.max(LlmCall.id))) or 0
        verdicts = collect(session, args.limit, args.kind)
        control = [] if args.no_control else control_check(session)
        graded = session.scalars(
            select(LlmCall).where(LlmCall.id > before, LlmCall.purpose == "eval")
        ).all()

    if not verdicts:
        print("нечего проверять: в базе нет резюме, написанных моделью")
        return 1

    cost = sum(call.cost or 0.0 for call in graded)
    stats = metrics(verdicts)
    ok, total, percent = stats["total"]
    print(f"подтверждено {ok} из {total} утверждений ({percent}%), ${cost:.6f}")
    for name in ("critic", "user", "likes", "dislikes"):
        sub_ok, sub_total, sub_percent = stats[name]
        print(f"  {name:9} {sub_ok:>3} / {sub_total:<3} {sub_percent}%")
    for verdict in verdicts:
        if not verdict.supported:
            print(f"  НЕ ПОДТВЕРЖДЕНО [{verdict.slug}/{verdict.kind}] {verdict.claim}")
    if control:
        rejected = sum(1 for v in control if not v.supported)
        print(f"контроль: судья отверг {rejected} из {len(control)} выдумок")
        if rejected < len(control):
            print("  ВНИМАНИЕ: судья засчитал выдумку, доли выше завышены")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_report(verdicts, stats, cost, len(graded), control), "utf-8")
        print(f"отчёт: {args.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
