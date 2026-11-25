# Oxbridge Health Engineering Challenge: Prior Authorization API

Before a patient can have certain procedures, their doctor's office must ask the health plan for approval. This is called a **prior authorization**.

Imagine you've just joined our team. Our Utilization Management (UM) team needs a new prior authorization service, and you're building the backend API. Below is a note from your tech lead, followed by the UM team's requirements in their own words.

---

## Note from your tech lead

- **Stack:** Python and PostgreSQL. It should be runnable on AWS; you don't need to deploy it. Framework and libraries are your call.
- **AI tools:** Use AI coding tools; we do.
- **Identity:** our identity service issues each caller a token containing their user ID and role. Fake it however you like. You don't need sign-up or login.
- **Data:** seed data for members, providers, reviewers, coverage and patient–practice links is fine. You don't need screens or endpoints to manage it.
- We'll be living with this code for years, and the UM team changes its mind often. Write it the way you'd want to find it.
- These requirements came straight from the business. If something is unclear or seems to contradict something else, make a call and write it down, as you would on the job.

---

## Requirements from the Utilization Management team

### Who uses it
- **Members:** our patients.
- **Provider offices:** doctors' offices submitting requests for their patients.
- **Reviewers:** our nurses and physicians who decide requests.
- **Operations admins:** they keep the work moving.

Each of these groups has its own roadmap. Their needs change independently, often on different teams, and we'll be adding new groups (auditors are next).

### Submitting a request
- Offices submit requests for their patients, but only for patients linked to their practice.
- A request covers one or more procedures (CPT or HCPCS codes, each with a number of units), a diagnosis (ICD-10), and the planned date of service. Offices can attach a clinical note.
- We don't accept requests for procedures that have already happened; those go through retro review, which isn't part of this project. We also don't accept requests more than a year out.
- The patient must have active coverage on the date of service.
- Our provider offices are nationwide, from Maine to Hawaii.
- More than 30 units for a single procedure is almost always a typo.
- Offices complain constantly that when we reject a submission, they can't tell why. Tell them exactly what to fix.

### Reviewing a request
- Reviewers pick up new requests from the queue. Once a reviewer picks one up, it's theirs until they decide it or an admin hands it to someone else.
- Some of our physicians also see patients. A reviewer must never review a request for a patient they treat.
- Reviewers approve or deny each procedure individually, and any denial needs a reason. They submit their decision for the whole request at once.
- Decisions are final. Appeals are a separate process and not part of this project.

### Who sees what
- Members can see their requests and whether each procedure was approved. Clinical notes and reviewer reasoning are internal, and members must never see them.
- Offices see the requests they submitted.
- Admins see everything.

### Compliance
- We're audited regularly. For any request, we must be able to show every step: who did what, and when.
- Last year an audit found a request marked "approved" with no record of who approved it. That cannot happen again.
- We're a HIPAA-covered entity.

### Support
- When an office calls to say "my submission didn't go through," our support desk needs to find out what happened.

### Partners
- Several large provider groups will connect their own systems directly to this service. Their IT teams will build from our documentation alone.

### Wishlist (only if you have time)
- Offices would love to be notified when a decision is made.
- Team leads want to see how many open requests each reviewer has.

---

## What to submit

Create your own copy of this repo (**Use this template → Private**), build your solution there using synthetic data only, and add `rgavrilov5` as a collaborator when you're done.

Your README should include:
- **How to run it.** Step-by-step instructions to run it locally.
- **Decisions and assumptions.** In particular, anything in the requirements you found unclear or contradictory, and what you decided.
- **How you used AI.** What you asked it to do, where it got things wrong, and what you changed.

## What happens next

If your submission moves forward, we'll schedule a 45-minute session where you walk us through your code and make a small change to it live. Please be ready to explain any line in the repo.
