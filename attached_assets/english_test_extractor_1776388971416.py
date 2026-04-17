"""
English Test Extractor - Production Ready
Extracts IELTS, PTE, TOEFL, CAE, DET with multi-page and tab support
"""

import re
from typing import Dict, Optional, List
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import aiohttp


class EnglishTestExtractor:
    """
    Extract English tests with TAB/SECTION awareness
    
    CRITICAL: Many sites put requirements in tabs or separate sections
    Supports: IELTS, PTE, TOEFL, CAE, Duolingo (DET)
    """
    
    async def extract(
        self, 
        html: str, 
        url: str, 
        country: str,
        university_requirements_url: Optional[str] = None
    ) -> Dict:
        """
        Extract with multi-strategy approach:
        1. Check if requirements are in tabs/sections
        2. Extract from visible content
        3. Check university-level requirements page
        4. Check linked requirements page
        5. Parse tables for structured data
        """
        
        soup = BeautifulSoup(html, 'html.parser')
        result = {}
        
        # ═══════════════════════════════════════════════════════
        # STRATEGY 1: Find Entry Requirements Section/Tab
        # ═══════════════════════════════════════════════════════
        
        requirements_section = self._find_requirements_section(soup)
        
        if requirements_section:
            print("[INFO] Found Entry Requirements section")
            
            # Try table extraction first (most reliable)
            table_data = self._extract_from_table(requirements_section)
            if table_data and table_data.get('ielts_overall'):
                result.update(table_data)
                return result
            
            # Fallback to text extraction
            section_html = str(requirements_section)
            text_data = self._extract_all_tests(section_html)
            if text_data and text_data.get('ielts_overall'):
                result.update(text_data)
                return result
        
        # ═══════════════════════════════════════════════════════
        # STRATEGY 2: University-level requirements page
        # ═══════════════════════════════════════════════════════
        
        if university_requirements_url and not result.get('ielts_overall'):
            print(f"[INFO] Fetching university-level requirements from: {university_requirements_url}")
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(university_requirements_url, timeout=15) as response:
                        uni_html = await response.text()
                
                uni_soup = BeautifulSoup(uni_html, 'html.parser')
                
                # Find requirements section
                uni_req_section = self._find_requirements_section(uni_soup)
                
                if uni_req_section:
                    table_data = self._extract_from_table(uni_req_section)
                    if table_data and table_data.get('ielts_overall'):
                        print("[SUCCESS] Extracted from university-level requirements table")
                        result.update(table_data)
                        return result
                
                # Try full page
                text_data = self._extract_all_tests(uni_html)
                if text_data and text_data.get('ielts_overall'):
                    print("[SUCCESS] Extracted from university-level requirements page")
                    result.update(text_data)
                    return result
            
            except Exception as e:
                print(f"[WARNING] Failed to fetch university requirements: {e}")
        
        # ═══════════════════════════════════════════════════════
        # STRATEGY 3: Linked requirements page from course
        # ═══════════════════════════════════════════════════════
        
        if not result.get('ielts_overall'):
            req_url = self._find_requirements_page_link(soup, url)
            
            if req_url:
                print(f"[INFO] Fetching linked requirements from: {req_url}")
                
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(req_url, timeout=15) as response:
                            req_html = await response.text()
                    
                    req_soup = BeautifulSoup(req_html, 'html.parser')
                    
                    # Try table extraction
                    table_data = self._extract_from_table(req_soup)
                    if table_data:
                        result.update(table_data)
                        return result
                    
                    # Try text extraction
                    text_data = self._extract_all_tests(req_html)
                    result.update(text_data)
                
                except Exception as e:
                    print(f"[WARNING] Failed to fetch linked requirements: {e}")
        
        # ═══════════════════════════════════════════════════════
        # STRATEGY 4: Full page text extraction (last resort)
        # ═══════════════════════════════════════════════════════
        
        if not result.get('ielts_overall'):
            print("[INFO] Trying full page text extraction")
            full_page_data = self._extract_all_tests(html)
            result.update(full_page_data)
        
        return result
    
    
    def _find_requirements_section(self, soup: BeautifulSoup) -> Optional[BeautifulSoup]:
        """
        Find Entry Requirements section/tab in the page
        
        Common patterns:
        - <div id="entry-requirements">
        - <section class="requirements">
        - Tab content with "Entry Requirements" heading
        """
        
        # Strategy 1: ID or class containing "requirements"
        patterns = [
            {'id': re.compile(r'entry.*requirements?', re.I)},
            {'id': re.compile(r'requirements?', re.I)},
            {'class': re.compile(r'entry.*requirements?', re.I)},
            {'class': re.compile(r'requirements?', re.I)},
        ]
        
        for pattern in patterns:
            section = soup.find(['div', 'section', 'article'], pattern)
            if section:
                return section
        
        # Strategy 2: Find heading with "Entry Requirements"
        headings = soup.find_all(['h1', 'h2', 'h3', 'h4'], 
                                 string=re.compile(r'entry\s+requirements?', re.I))
        
        for heading in headings:
            parent = heading.find_parent(['div', 'section', 'article'])
            if parent:
                return parent
        
        # Strategy 3: Tab panels
        tab_panels = soup.find_all(['div'], {'role': 'tabpanel'})
        
        for panel in tab_panels:
            panel_text = panel.get_text().lower()
            if 'ielts' in panel_text or 'english' in panel_text:
                return panel
        
        return None
    
    
    def _extract_from_table(self, soup: BeautifulSoup) -> Dict:
        """
        Extract English requirements from HTML table
        
        Example table:
        | Test Type  | Requirement           |
        |------------|-----------------------|
        | IELTS      | Overall 6.0, min 5.5  |
        | PTE        | Overall 50            |
        """
        
        result = {}
        tables = soup.find_all('table')
        
        for table in tables:
            rows = table.find_all('tr')
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                
                if len(cells) < 2:
                    continue
                
                test_type = cells[0].get_text(strip=True).lower()
                requirement = cells[1].get_text(strip=True)
                
                # IELTS
                if 'ielts' in test_type:
                    ielts = self._parse_ielts_requirement(requirement)
                    if ielts:
                        result.update({
                            'ielts_overall': ielts.get('overall'),
                            'ielts_listening': ielts.get('listening'),
                            'ielts_reading': ielts.get('reading'),
                            'ielts_writing': ielts.get('writing'),
                            'ielts_speaking': ielts.get('speaking')
                        })
                
                # PTE
                elif 'pte' in test_type or 'pearson' in test_type:
                    pte = self._parse_pte_requirement(requirement)
                    if pte:
                        result.update({
                            'pte_overall': pte.get('overall'),
                            'pte_listening': pte.get('listening'),
                            'pte_reading': pte.get('reading'),
                            'pte_writing': pte.get('writing'),
                            'pte_speaking': pte.get('speaking')
                        })
                
                # TOEFL
                elif 'toefl' in test_type:
                    toefl = self._parse_toefl_requirement(requirement)
                    if toefl:
                        result.update({
                            'toefl_overall': toefl.get('overall'),
                            'toefl_listening': toefl.get('listening'),
                            'toefl_reading': toefl.get('reading'),
                            'toefl_writing': toefl.get('writing'),
                            'toefl_speaking': toefl.get('speaking')
                        })
                
                # CAE
                elif 'cae' in test_type or 'cambridge' in test_type:
                    match = re.search(r'(\d+)', requirement)
                    if match:
                        result['cae_score'] = int(match.group(1))
                
                # Duolingo
                elif 'duolingo' in test_type or 'det' in test_type:
                    match = re.search(r'(\d+)', requirement)
                    if match:
                        result['duolingo_score'] = int(match.group(1))
        
        return result
    
    
    def _parse_ielts_requirement(self, text: str) -> Optional[Dict]:
        """
        Parse IELTS requirement text
        
        Examples:
        - "Overall Band Score 6.0 with a minimum sub-score of 5.5"
        - "6.5 (no band less than 6.0)"
        - "Overall 6.0, minimum 5.5"
        """
        
        # Pattern 1: "Overall X with minimum Y"
        pattern1 = r'(?:overall|band)?\s*(?:score)?\s*([\d.]+)[^\d]*(minimum|no\s+(?:band|score)\s+(?:less|lower)\s+than|sub-score)[^\d]*([\d.]+)'
        match = re.search(pattern1, text, re.I)
        
        if match:
            overall = float(match.group(1))
            min_band = float(match.group(3))
            
            return {
                'overall': overall,
                'listening': min_band,
                'reading': min_band,
                'writing': min_band,
                'speaking': min_band
            }
        
        # Pattern 2: Just overall score
        pattern2 = r'(?:overall)?\s*(?:score)?\s*([\d.]+)'
        match = re.search(pattern2, text, re.I)
        
        if match:
            return {'overall': float(match.group(1))}
        
        return None
    
    
    def _parse_pte_requirement(self, text: str) -> Optional[Dict]:
        """Parse PTE requirement text"""
        
        # Pattern 1: "Overall X with minimum Y"
        pattern1 = r'(?:overall)?\s*(?:score)?\s*(\d+)[^\d]*(minimum|no\s+(?:skill|score)\s+(?:less|lower|below)\s+than)[^\d]*(\d+)'
        match = re.search(pattern1, text, re.I)
        
        if match:
            overall = int(match.group(1))
            min_score = int(match.group(3))
            
            return {
                'overall': overall,
                'listening': min_score,
                'reading': min_score,
                'writing': min_score,
                'speaking': min_score
            }
        
        # Pattern 2: Just overall
        pattern2 = r'(?:overall)?\s*(?:score)?\s*(\d+)'
        match = re.search(pattern2, text, re.I)
        
        if match:
            return {'overall': int(match.group(1))}
        
        return None
    
    
    def _parse_toefl_requirement(self, text: str) -> Optional[Dict]:
        """Parse TOEFL requirement text"""
        
        # Pattern 1: "Overall X with minimum Y"
        pattern1 = r'(\d+)[^\d]*(minimum|no\s+(?:section|score)\s+(?:less|lower|below)\s+than)[^\d]*(\d+)'
        match = re.search(pattern1, text, re.I)
        
        if match:
            overall = int(match.group(1))
            min_score = int(match.group(3))
            
            return {
                'overall': overall,
                'listening': min_score,
                'reading': min_score,
                'writing': min_score,
                'speaking': min_score
            }
        
        # Pattern 2: Just overall
        pattern2 = r'(\d+)\s*(?:overall)?'
        match = re.search(pattern2, text, re.I)
        
        if match:
            return {'overall': int(match.group(1))}
        
        return None
    
    
    def _extract_all_tests(self, html: str) -> Dict:
        """Fallback: Extract from plain text"""
        
        text = re.sub(r'<[^>]+>', ' ', html)
        result = {}
        
        # IELTS
        ielts_patterns = [
            r'IELTS[:\s]*(?:Academic)?[:\s]*([\d.]+)[^\d]*(no\s+band\s+(?:less|lower)\s+than|minimum)[^\d]*([\d.]+)',
            r'IELTS[:\s]*(?:Academic)?[:\s]*([\d.]+)',
        ]
        
        for pattern in ielts_patterns:
            match = re.search(pattern, text, re.I)
            if match:
                overall = float(match.group(1))
                min_band = float(match.group(3)) if len(match.groups()) >= 3 else None
                
                result['ielts_overall'] = overall
                if min_band:
                    result['ielts_listening'] = min_band
                    result['ielts_reading'] = min_band
                    result['ielts_writing'] = min_band
                    result['ielts_speaking'] = min_band
                break
        
        # PTE
        pte_pattern = r'PTE[:\s]*(?:Academic)?[:\s]*(\d+)'
        match = re.search(pte_pattern, text, re.I)
        if match:
            result['pte_overall'] = int(match.group(1))
        
        # TOEFL
        toefl_pattern = r'TOEFL[:\s]*(?:iBT)?[:\s]*(\d+)'
        match = re.search(toefl_pattern, text, re.I)
        if match:
            result['toefl_overall'] = int(match.group(1))
        
        # CAE
        cae_pattern = r'CAE[:\s]*(?:Cambridge)?[:\s]*(\d+)'
        match = re.search(cae_pattern, text, re.I)
        if match:
            result['cae_score'] = int(match.group(1))
        
        # Duolingo
        duolingo_pattern = r'Duolingo[:\s]*(?:English\s+Test)?[:\s]*(\d+)'
        match = re.search(duolingo_pattern, text, re.I)
        if match:
            result['duolingo_score'] = int(match.group(1))
        
        return result
    
    
    def _find_requirements_page_link(self, soup: BeautifulSoup, base_url: str) -> Optional[str]:
        """Find link to separate requirements page"""
        
        for link in soup.find_all('a', href=True):
            href = link.get('href')
            text = link.get_text(strip=True).lower()
            
            if any(kw in text for kw in ['requirement', 'admission', 'entry', 'eligibility']):
                return urljoin(base_url, href)
        
        return None
