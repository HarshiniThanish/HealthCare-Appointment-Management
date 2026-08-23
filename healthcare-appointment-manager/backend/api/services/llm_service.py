"""
LLM integration — pre-visit symptom summary and post-visit patient-friendly summary.

Design principle: THIS MODULE NEVER RAISES. Every public function catches all
exceptions (network errors, API errors, malformed JSON, unexpected values) and
records status='failed' + error_message on the model instead. Booking and the
clinical workflow must never break because the LLM is down or returns garbage —
that's an explicit requirement in the assignment brief.

Uses Anthropic's Messages API. Swap _call_llm's implementation to hit a different
provider (OpenAI, etc.) without touching the calling code — provider details are
fully isolated here.
"""

import json
import logging
import os
from typing import Optional

import anthropic
from django.utils import timezone

from core.models import Appointment, PreVisitSummary, PostVisitSummary

logger = logging.getLogger(__name__)

LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
_client: Optional[anthropic.Anthropic] = None

PRE_VISIT_PROMPT = (
    "Analyse these symptoms and return STRICT JSON only — no prose, no markdown fences — "
    'with exactly these keys: {{"urgency_level": "Low|Medium|High", '
    '"chief_complaint": "<one sentence>", "suggested_questions": ["q1", "q2", "q3"]}}. '
    "Symptoms: {symptoms}"
)

POST_VISIT_PROMPT = (
    "Convert these clinical notes into a patient-friendly summary. Return STRICT JSON "
    'only — no prose, no markdown fences — with exactly these keys: '
    '{{"patient_friendly_summary": "<plain-language summary>", '
    '"medication_schedule": [{{"medication": "...", "dosage": "...", "frequency": "...", '
    '"duration": "..."}}], "follow_up_steps": "<what the patient should do next>"}}. '
    "Notes: {notes}"
)


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _call_llm(prompt: str) -> str:
    client = _get_client()
    response = client.messages.create(
        model=LLM_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


def _parse_json_response(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())


def generate_pre_visit_summary(appointment: Appointment) -> PreVisitSummary:
    """Generate the AI pre-visit summary from the patient's symptom form.
    Called right after booking is confirmed. Never raises."""
    summary, _ = PreVisitSummary.objects.get_or_create(appointment=appointment)

    try:
        symptoms_text = appointment.symptom_form.symptoms_text
        raw = _call_llm(PRE_VISIT_PROMPT.format(symptoms=symptoms_text))
        parsed = _parse_json_response(raw)

        urgency = parsed.get("urgency_level")
        if urgency not in ("Low", "Medium", "High"):
            raise ValueError(f"Unexpected urgency_level value: {urgency!r}")
        questions = parsed.get("suggested_questions", [])
        if not isinstance(questions, list):
            raise ValueError("suggested_questions was not a list.")

        summary.status = "success"
        summary.urgency_level = urgency
        summary.chief_complaint = parsed.get("chief_complaint", "")
        summary.suggested_questions = questions[:3]
        summary.raw_llm_response = raw
        summary.error_message = ""
        summary.generated_at = timezone.now()

    except Exception as exc:
        # Covers: missing API key, network failure, non-JSON response, missing
        # symptom_form, unexpected schema — all funnel into the same safe path.
        logger.warning("Pre-visit LLM summary failed for appointment %s: %s", appointment.id, exc)
        summary.status = "failed"
        summary.error_message = str(exc)[:1000]
        summary.generated_at = timezone.now()

    summary.save()
    return summary


def generate_post_visit_summary(appointment: Appointment) -> PostVisitSummary:
    """Generate the patient-friendly post-visit summary from the doctor's clinical notes.
    Called right after the doctor submits post-visit notes. Never raises."""
    summary, _ = PostVisitSummary.objects.get_or_create(appointment=appointment)

    try:
        notes = appointment.post_visit_note.clinical_notes
        raw = _call_llm(POST_VISIT_PROMPT.format(notes=notes))
        parsed = _parse_json_response(raw)

        summary.status = "success"
        summary.patient_friendly_summary = parsed.get("patient_friendly_summary", "")
        summary.medication_schedule = parsed.get("medication_schedule", [])
        summary.follow_up_steps = parsed.get("follow_up_steps", "")
        summary.raw_llm_response = raw
        summary.error_message = ""
        summary.generated_at = timezone.now()

    except Exception as exc:
        logger.warning("Post-visit LLM summary failed for appointment %s: %s", appointment.id, exc)
        summary.status = "failed"
        summary.error_message = str(exc)[:1000]
        summary.generated_at = timezone.now()

    summary.save()
    return summary


def retry_failed_summaries() -> int:
    """Background-job entry point: re-attempt any summary stuck in 'failed'.
    Safe to run repeatedly — get_or_create + overwrite means no duplicates."""
    count = 0
    for summary in PreVisitSummary.objects.filter(status="failed").select_related("appointment"):
        generate_pre_visit_summary(summary.appointment)
        count += 1
    for summary in PostVisitSummary.objects.filter(status="failed").select_related("appointment"):
        generate_post_visit_summary(summary.appointment)
        count += 1
    return count
