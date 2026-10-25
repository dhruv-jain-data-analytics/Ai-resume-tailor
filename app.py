"""
AI Resume Tailor (PDF in -> PDF out, same layout)

Upload your master resume PDF + paste a job description. The app rewrites only
the bullets / summary / skills lines, in place, so your layout, fonts, colours
and hyperlinks stay exactly as they are. Output: a PDF (a 1-page resume stays 1 page).

Run:  streamlit run app.py
"""

import re
import time

import streamlit as st

import pdf_tailor as pt

DEFAULT_MODEL = "gemini-3.8-flash"

st.set_page_config(page_title="AI Resume Tailor", page_icon="📄", layout="wide")


@st.cache_data(show_spinner=False)
def analyze_cached(pdf_bytes: bytes):
    return pt.analyze_pdf(pdf_bytes)


st.title("📄 AI Resume Tailor")
st.caption("Your master resume PDF + a job description → the same resume, tailored. "
           "Layout, fonts, colours and links stay exactly as they are.")

with st.sidebar:
    st.header("⚙️ Settings")
    api_key = st.text_input("Gemini API Key", type="password",
                            help="Free key: https://aistudio.google.com/app/apikey")
    model_name = st.text_input(
        "Gemini model", value=DEFAULT_MODEL,
        help="If you ever get a 'model not found' error, type the newest Flash "
             "model name here. No code change needed.")
    st.markdown("**Only if your LinkedIn / GitHub text has no link behind it**")
    linkedin_url = st.text_input("LinkedIn URL", placeholder="https://linkedin.com/in/...")
    github_url = st.text_input("GitHub URL", placeholder="https://github.com/...")
    ai_check = st.checkbox("Extra AI fact-check (slower, +5-10 sec)", value=False)
    st.divider()
    st.caption("🔒 Nothing is stored. Your resume text goes to the Gemini API only "
               "when you click the button.")

col_left, col_right = st.columns(2)

pdf_bytes = None
resume = None
with col_left:
    st.subheader("1 · Your master resume (PDF)")
    uploaded = st.file_uploader("Upload your resume as a PDF", type=["pdf"])
    if uploaded is not None:
        pdf_bytes = uploaded.getvalue()
        file_id = f"{uploaded.name}-{uploaded.size}"
        if st.session_state.get("_file_id") != file_id:
            st.session_state["_file_id"] = file_id
            st.session_state.pop("result", None)
        try:
            resume = analyze_cached(pdf_bytes)
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read this PDF: {e}")
        if resume:
            for w in resume.warnings:
                st.warning(w)
            if resume.has_text:
                editable = [p for p in resume.paragraphs if p.editable]
                st.success(f"Resume read. {len(editable)} lines can be tailored "
                           f"(summary, skills, bullets). Headings, titles, companies, "
                           f"dates, education and contact details are never touched.")
                with st.expander("See exactly which lines the AI may change"):
                    for p in editable:
                        st.markdown(f"**{p.kind}** · {p.section.title()}  \n{p.text}")

with col_right:
    st.subheader("2 · Job description")
    jd_text = st.text_area("Paste the full job description", height=330,
                           placeholder="Paste the job posting here...")

go = st.button("✨ Tailor my resume", type="primary", width="stretch")

if go:
    if not api_key:
        st.error("Enter your Gemini API key in the sidebar first.")
    elif resume is None or not resume.has_text:
        st.error("Upload a text-based resume PDF first.")
    elif not any(p.editable for p in resume.paragraphs):
        st.error("I could not find bullet points / summary / skills lines to tailor "
                 "in this PDF.")
    elif len(jd_text.strip()) < 150:
        st.error("Job description looks too short. Paste the full posting.")
    else:
        t0 = time.time()
        try:
            with st.status("Working on it...", expanded=True) as status:
                st.write("1/3 AI is tailoring your resume to the job (one quick call)...")
                data = pt.llm_tailor(api_key, model_name, jd_text, resume)

                st.write("2/3 Rewriting the PDF in place...")
                new_pdf, applied, skipped, links_added = pt.apply_edits(
                    pdf_bytes, resume, data["edits"], linkedin_url, github_url)

                st.write("3/3 Checking facts and keywords...")
                matched, missing = pt.keyword_match(
                    list(data.get("keywords", [])) + list(data.get("must_have_skills", [])),
                    resume.full_text)
                new_numbers, new_terms = pt.fact_check(resume.full_text, applied)
                unsupported = (pt.ai_fact_check(api_key, model_name, resume.full_text, applied)
                               if ai_check and applied else "off")

                st.session_state["result"] = {
                    "pdf": new_pdf, "applied": applied, "skipped": skipped,
                    "links_added": links_added, "data": data,
                    "matched": matched, "missing": missing,
                    "new_numbers": new_numbers, "new_terms": new_terms,
                    "unsupported": unsupported, "seconds": round(time.time() - t0, 1),
                    "previews": pt.render_pages(new_pdf),
                }
                status.update(label=f"Done in {round(time.time() - t0, 1)} sec",
                              state="complete", expanded=False)
        except Exception as e:  # noqa: BLE001
            st.error(f"Something went wrong: {e}")

result = st.session_state.get("result")
if result:
    role = result["data"].get("role_title") or "role"
    safe = re.sub(r"[^A-Za-z0-9]+", "_", role).strip("_") or "role"

    if not result["applied"]:
        st.warning("No lines needed to change, or the new wording did not fit in the "
                   "original space. Your resume may already match this job well.")

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📄 Preview & download", "🔁 What changed", "🎯 Match report", "🔎 Fact check"])

    with tab1:
        st.download_button("⬇️ Download tailored resume (PDF)", data=result["pdf"],
                           file_name=f"Resume_{safe}.pdf", mime="application/pdf",
                           type="primary", width="stretch")
        st.caption(f"{len(result['applied'])} lines tailored in {result['seconds']} sec"
                   + (f" · {len(result['skipped'])} kept as original (new wording did "
                      f"not fit)" if result["skipped"] else ""))
        for img in result["previews"]:
            st.image(img, width="stretch")

    with tab2:
        if not result["applied"]:
            st.info("Nothing changed.")
        for a in result["applied"]:
            with st.container(border=True):
                st.markdown(f"**{a['section'].title()}**")
                st.markdown(f"~~{a['old']}~~")
                st.markdown(f"➡️ {a['new']}")

    with tab3:
        total = len(result["matched"]) + len(result["missing"])
        score = round(100 * len(result["matched"]) / total) if total else 0
        m1, m2, m3 = st.columns(3)
        m1.metric("Keyword match", f"{score}%")
        m2.metric("Found in your resume", len(result["matched"]))
        m3.metric("Not in your resume", len(result["missing"]))
        st.markdown("**✅ Already in your resume:** " + (", ".join(result["matched"]) or "—"))
        st.markdown("**⚠️ Asked by the job, NOT in your resume:** " +
                    (", ".join(result["missing"]) or "—"))
        st.info("Missing items were deliberately NOT added. Use them to judge the fit "
                "or to decide what to learn next.")

    with tab4:
        st.markdown("**Numbers that are not in your master resume**")
        if result["new_numbers"]:
            st.warning("Check these: " + ", ".join(result["new_numbers"]))
        else:
            st.success("No new numbers were introduced.")
        st.markdown("**Tool / proper-noun words that are not in your master resume**")
        if result["new_terms"]:
            st.warning("Check these: " + ", ".join(result["new_terms"]))
        else:
            st.success("No new tool or skill names were introduced.")
        ua = result["unsupported"]
        if ua != "off":
            st.markdown("**AI reviewer**")
            if ua is None:
                st.info("The AI reviewer could not run this time.")
            elif ua:
                for item in ua:
                    st.warning(f"**{item.get('claim', '')}** — {item.get('why', '')}")
            else:
                st.success("Everything is supported by your master resume.")
        st.caption("Always read the final PDF yourself before applying.")
