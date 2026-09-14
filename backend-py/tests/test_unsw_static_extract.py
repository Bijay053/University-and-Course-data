from app.services.scraper.unsw_static_extract import apply_unsw_static_extraction


URL = "https://www.unsw.edu.au/study/undergraduate/bachelor-of-aviation-flying"


def test_unsw_prefers_international_first_year_fee_and_reads_english_options():
    html = r'''
      <dt><div class="cmp-contentfragment__element--domesticFull">
        Indicative full fee to complete degree</div></dt><dd>$181,000*</dd>
      <dt><div class="cmp-contentfragment__element--internationalAnnual">
        Indicative first year full fee</div></dt><dd>$62,500*</dd>
      <dt><div class="cmp-contentfragment__element--internationalFull">
        Indicative full fee to complete degree</div></dt><dd>$348,000*</dd>
      <script>
      window.engRequirementsConfig = "{\x22toeflIbt\x22:\x2290.0 Overall (Min: 22.0 in reading, 22.0 in listening, 22.0 in speaking and 23.0 in writing)\x22,\x22pearsonsTestOfEnglish\x22:\x2264.0 Overall (Min: 54.0 in listening)\x22,\x22ielts\x22:\x226.5 Overall (Min: 6.0 in listening, 6.0 in reading, 6.0 in writing and 6.0 in speaking)\x22,\x22c1AdvancedCambridge\x22:\x22176.0 Overall\x22}"
      </script>
    '''

    result = apply_unsw_static_extraction(URL, html)

    assert result["international_fee"] == 62500
    assert result["fee_term"] == "Annual"
    assert result["ielts_overall"] == 6.5
    assert result["ielts_listening"] == 6.0
    assert result["pte_overall"] == 64.0
    assert result["toefl_overall"] == 90.0
    assert result["toefl_writing"] == 23.0
    assert result["cambridge_overall"] == 176.0


def test_unsw_does_not_use_full_course_fee_when_annual_is_absent():
    html = """
      <dt><div class="cmp-contentfragment__element--internationalFull">
        Indicative full fee to complete degree</div></dt><dd>$348,000*</dd>
    """
    assert apply_unsw_static_extraction(URL, html).get("international_fee") is None