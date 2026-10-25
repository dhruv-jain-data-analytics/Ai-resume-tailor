# AI Resume Tailor (PDF in -> PDF out, same layout)

Upload your master resume as a PDF, paste a job description, get the same
resume back as a PDF, tailored to that job.

* Your layout, fonts, colours, lines and hyperlinks (LinkedIn / GitHub) stay exactly
  as they are. Only the text of bullets, the summary and skills lines is replaced,
  in the same place, size and colour.
* Headings, job titles, companies, dates, education and contact details are never touched.
* The AI may only reword / reorder what is already in your resume. It never adds
  skills, tools, numbers or employers. A fact check flags anything suspicious.
* A 1-page resume stays 1 page (new text must fit in the original space; if it
  does not, that line is kept as it was).
* One AI call only, so it takes seconds, not minutes.

## Run locally
    pip install -r requirements.txt
    streamlit run app.py
(Windows: if `streamlit` is not recognised, use `python -m streamlit run app.py`)

## Free Gemini API key
https://aistudio.google.com/app/apikey  -> paste in the sidebar.

## Model name
The sidebar has a "Gemini model" box (default gemini-3.8-flash). If Google retires it
and you see "model not found", type the newest Flash model name there. No code change.

## Deploy (free, Streamlit Community Cloud)
Push app.py, pdf_tailor.py, requirements.txt, packages.txt, README.md to a new GitHub repo,
then share.streamlit.io -> New app -> main file: app.py.
`packages.txt` installs Calibri/Arial/Times look-alike fonts on the server so edited
lines match your original fonts. On Windows your own fonts are used automatically.

## Best results
* Use a text-based PDF (exported from Word / Google Docs / Canva). Scanned or image PDFs have no text.
* Works for single-column and sidebar (two-column) layouts.
* Lines with mixed bold/normal words inside one bullet are rewritten in the bullet's main style.
