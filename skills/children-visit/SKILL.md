---
name: children-visit
description: Helps an adult book or prepare a child's eye visit at NetraCare — children under 16 see Dr. Vikram Menon (Paediatric), with what to bring, how long it takes and the indicative cost. Use when the patient is under 16 (year of birth), or when they ask about a child's eyes, a child's eye test, or which doctor a child should see.
---

# Children's eye visit

Children under 16 are seen by the Paediatric doctor, whatever the problem.
This skill gives the clinic's own facts for a child's visit and then offers
to find a slot. It never names a condition and never suggests treatment.

## Who sees the child

- Doctor: Dr. Vikram Menon (Paediatric, Squint, General).
- OPD days: Monday, Tuesday, Thursday and Saturday, morning and afternoon.
  Not at the clinic on Wednesday and Friday.
- A parent or guardian must stay with the child for the whole visit.

## Children's eye check (from procedures.md)

- What happens: vision tested with pictures or letters to suit the child's
  age, a squint check, and a glasses power check with drops that relax
  focusing.
- Time at the clinic: about 1½ hours, because the drops take 40 minutes to work.
- Before: a parent or guardian must come. Bring the child's glasses and any
  school eye-check report. A snack and a favourite toy help.
- After: the child's near vision is blurred and the pupils stay large for up
  to a day. Sunglasses or a cap help outdoors.
- Indicative cost: ₹900, including the consultation.

## Steps

1. Confirm the child is under 16 (year of birth, or the patient says so).
   If they are asking about themselves and they are 16 or over, this skill
   does not apply — use the normal speciality guide instead.
2. Share who the child sees (Dr. Menon), that a parent or guardian must
   stay, the time at the clinic, what to bring and the indicative cost —
   only the parts the patient asked about, in short bullets.
3. Offer to find a Paediatric slot with Dr. Menon. If they want that, call
   `find_slots` with speciality Paediatric and offer free times; book only
   after they confirm, with a short reason (for example "Child eye check").
4. If they only wanted the information, stop there. Do not book unless they
   ask.

## Rules

- Never name a condition, never suggest treatment, medicine or drops, and
  never read any report.
- Never invent a cost, day or duration — only what is written above (and
  what `search_clinic_docs` returns if asked for more).
- Give the source as: Skill: children-visit.
