from naukri_agent.jobs.models import compute_content_fingerprint, extract_external_id


def test_extract_external_id_from_typical_naukri_url():
    url = "https://www.naukri.com/job-listings-python-developer-pune-020124500123"
    assert extract_external_id(url) == "020124500123"


def test_extract_external_id_handles_query_string():
    url = "https://www.naukri.com/job-listings-data-scientist-pune-987654?src=search"
    assert extract_external_id(url) == "987654"


def test_extract_external_id_returns_none_when_no_numeric_id():
    url = "https://www.naukri.com/some-page-without-an-id"
    assert extract_external_id(url) is None


def test_content_fingerprint_stable_for_identical_input():
    fp1 = compute_content_fingerprint("Data Scientist", "Acme Corp", "Some description.")
    fp2 = compute_content_fingerprint("Data Scientist", "Acme Corp", "Some description.")
    assert fp1 == fp2


def test_content_fingerprint_normalizes_case_and_whitespace():
    fp1 = compute_content_fingerprint("Data Scientist", "Acme Corp", "Some   description.")
    fp2 = compute_content_fingerprint(
        "  data scientist  ", "ACME CORP", "some\ndescription."
    )
    assert fp1 == fp2


def test_content_fingerprint_changes_with_description():
    fp1 = compute_content_fingerprint("Data Scientist", "Acme Corp", "Description A.")
    fp2 = compute_content_fingerprint("Data Scientist", "Acme Corp", "Description B.")
    assert fp1 != fp2
