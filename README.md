# My Job Finder

Every morning this project looks for data analyst jobs in any industry, removes the fake, agency, senior and old ones, scores each job against my skills, and updates one web page that I can open on my phone.

It is free. It uses Python and regex (no paid AI), GitHub Actions (the daily robot) and GitHub Pages (the website).

---

## Setup (about 20 minutes, one time only)

### Step 1: Make a GitHub account
Go to github.com and sign up (skip this if you already have an account).

### Step 2: Create the repository (project folder)
1. Click **+** (top right), then **New repository**.
2. Name: `job-finder`. Choose **Public**, because free GitHub Pages needs a public repository.
3. Click **Create repository**.
4. Click **uploading an existing file**. Drag in **all** the files and folders from the zip, including the `.github` folder.
   - Tip: the `.github` folder is hidden on Mac. In Finder, press `Cmd + Shift + .` to show it.
5. Click **Commit changes**.

### Step 3: Turn on the website
1. In the repository, open **Settings**, then **Pages**.
2. Under "Branch", choose `main` and folder `/docs`, then click **Save**.
3. After 1–2 minutes your page address appears, for example:
   `https://YOUR-USERNAME.github.io/job-finder/`
   **Bookmark this on your phone.** This is the link you'll check every day.

### Step 4 (recommended): Add free Adzuna keys
Adzuna finds jobs from thousands of employers in every industry, including ones whose career sites can't be read directly.
1. Sign up at **developer.adzuna.com** (free). Copy your **App ID** and **App Key**.
2. In GitHub, go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Add two secrets:
   - Name `ADZUNA_APP_ID`, with your App ID as the value
   - Name `ADZUNA_APP_KEY`, with your App Key as the value

Secrets stay private, even though the repository is public.

### Step 5: Run it the first time
1. Open the **Actions** tab. If GitHub asks, click **I understand… enable workflows**.
2. Click **Daily job search**, then **Run workflow**, then **Run workflow**.
3. Wait about 3–5 minutes for the green check ✅. Then open your page.

From now on, it runs by itself every morning at about 6:15 AM.

---

## Changing what it looks for

Open `config.yaml` on GitHub, click the ✏️ pencil, edit it, then click **Commit changes**. The notes inside the file explain each part:

| I want to… | Change this in config.yaml |
|---|---|
| Add a skill I learned (e.g. Tableau) | `my_skills` |
| Search for other job titles | `search_terms` and `title_must_match` |
| Hide a kind of job | `title_must_not_match` |
| Add more towns | `locations_ok` |
| Give a bonus to an industry | `bonus_industries` |
| Block an agency | `blocked_companies` |
| Add an employer on Workday | `employers` (paste their careers web address) |

**Check the page after a change.** At the bottom of the page, open **"Which websites worked today?"**. ✅ means that site worked. ⚠️ means it failed; the note says why. One broken site never stops the others.

## How the score works

| Points | Why |
|---|---|
| 30 | Start: the job passed all the filters |
| +8 each (max 40) | Each of my skills the job mentions |
| −4 each (max −16) | Each common skill it asks for that I don't list |
| +5 | Industries I like (`bonus_industries`, healthcare by default) |
| +10 | Entry level words in the title (junior, associate, I, intern) |
| +10 or −15 | Years asked ≤ my max, or more than my max |
| +5 | Posted in the last 3 days |

All of this is in `score_job()` in `jobfinder.py`, so you can change the points yourself.

## Running it on your own computer (Python practice)

```
pip install -r requirements.txt
python jobfinder.py --sample     # practice run with fake jobs, no internet needed
python jobfinder.py              # real run
```
Then open `docs/index.html` in your browser.

## Good to know
- Some employers (like Hartford HealthCare) use career sites that block programs. Adzuna covers them instead. Jobs marked **via Adzuna** open through Adzuna first, then send you on to the employer.
- Always apply on the **employer's own website**. Real employers never ask you to pay a fee.
- Don't put your resume or personal details in this repository, because it is public.
