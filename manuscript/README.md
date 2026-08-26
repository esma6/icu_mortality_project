# Manuscript

`IEEE_JBHI_Submission_Manuscript.docx` is the submission-ready manuscript prepared for
the *IEEE Journal of Biomedical and Health Informatics*.

The manuscript reports aggregate results generated from the repository's tracked
outputs. The analysis snapshot cited in its data-availability statement is commit
`6fe3460679acea2eafe494025a3ab81050b6bd1d`.

The scripts in `analysis/` regenerate the confirmatory benchmark summaries:

```bash
python manuscript/analysis/analyze_validation.py
python manuscript/analysis/analyze_equal_capacity.py
```

No raw or patient-level MIMIC-III data are included in this directory or repository.
