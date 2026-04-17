"""
Research Validator - Production Ready
Fixed validation with junk detection and proper thresholds
"""

import re
import asyncio
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
import aiohttp
from urllib.parse import urlparse


class ResearchValidator:
    """
    Smart course page validation with proper scoring
    
    Fixes:
    - Relaxed validation thresholds (0.40 instead of 0.80)
    - Explicit junk page rejection
    - Special case for perfect URL + degree keyword
    - Comprehensive degree keyword list
    """
    
    def __init__(self):
        # Degree keywords - COMPREHENSIVE list
        self.degree_keywords = [
            # Undergraduate
            r'\bbachelor\b', r'\bb\.?\s*[a-z]+\b',
            r'\bassociate\s+degree\b',
            r'\bundergraduate\b',
            
            # Postgraduate taught
            r'\bmaster\b', r'\bm\.?\s*[a-z]+\b',
            r'\bpostgraduate(?!\s+research)\b',
            r'\bgraduate\s+certificate\b',
            r'\bgraduate\s+diploma\b',
            r'\bpg\s+cert\b', r'\bpg\s+dip\b',
            
            # Research
            r'\bphd\b', r'\bph\.?d\.?\b',
            r'\bdoctor(?:ate)?\b',
            r'\bpostgraduate\s+research\b',
            r'\bresearch\s+(?:master|degree)\b',
            
            # Honours
            r'\bhonours?\b', r'\bhons\.?\b',
            
            # Vocational
            r'\bdiploma\b', r'\bdip\.?\b',
            r'\bcertificate\s+(?:i{1,4}|[1-4])\b',
            r'\badvanced\s+diploma\b',
            r'\bcert\.?\s+(?:i{1,4}|[1-4])\b',
        ]
        
        # Course content signals
        self.course_signals = [
            r'\bduration\b', r'\bstudy\s+mode\b', r'\bintake\b',
            r'\bentry\s+requirements?\b', r'\bprerequisites?\b',
            r'\bcareer\s+(?:outcomes?|pathways?)\b',
            r'\bcourse\s+structure\b', r'\bunits?\b', r'\bsubjects?\b',
            r'\batar\b', r'\bgpa\b',
            r'\bdomestic\s+fee\b', r'\binternational\s+fee\b',
            r'\btuition\b', r'\bfees?\b',
            r'\bfull[- ]?time\b', r'\bpart[- ]?time\b',
            r'\bon[- ]?campus\b', r'\bonline\b', r'\bblended\b',
            r'\bsemester\b', r'\btrimester\b',
            r'\byear\s+\d+\b', r'\bstage\s+\d+\b',
            r'\bhow\s+to\s+apply\b', r'\bapplication\b',
            r'\bielts\b', r'\bpte\b', r'\btoefl\b',
            r'\bstart\s+date\b', r'\bcommencement\b',
        ]
        
        # CRITICAL: Explicit junk page patterns
        self.junk_patterns = [
            r'\binfo\s+night\b',              # "MBA Info Night"
            r'\bvirtual\s+info\s+night\b',    # "MBA Virtual Info Night"
            r'\bopen\s+day\b',                # "Open Day"
            r'^double\s+degrees?$',           # "Double Degrees" (exact match)
            r'^graduate\s+certificates?$',    # "Graduate Certificates"
            r'^undergraduate\s+courses?$',    # Category pages
            r'^postgraduate\s+courses?$',
            r'\bretains?\s+tier\b',           # News: "MBA Retains Tier One"
            r'\brankings?\b.*\b(?:ceo|magazine)\b',
            r'\bis\s+apac\s+accredited\b',    # News articles
            r'\bwhy\s+choose\b.*\buniversity\b',
            r'\bapply\s+now\b$',
            r'\bhow\s+to\s+apply\b$',         # Generic apply pages
        ]
        
        # Non-course signals (pages to reject)
        self.non_course_signals = [
            r'\baccommodation\s+options\b',
            r'\bstudent\s+services\b',
            r'\bbook\s+a\s+tour\b',
            r'\bcontact\s+us\b',
            r'\babout\s+(?:us|the\s+university)\b',
            r'\bwhy\s+choose\b',
            r'\bour\s+campus(?:es)?\b',
            r'\bstudent\s+(?:stories|testimonials)\b',
            r'\blatest\s+news\b',
            r'\bupcoming\s+events\b',
            r'\bwinners?\s+(?:and|&)\s+finalists?\b',
            r'\bstudy\s+tour\b',
            r'\bscholarships?\s+available\b',
        ]
        
        # Invalid URL patterns
        self.invalid_url_patterns = [
            r'/news/', r'/events/', r'/blog/', r'/media/',
            r'/about/', r'/contact/', r'/why-choose/',
            r'/accommodation/', r'/student-life/', r'/campus-life/',
            r'/visa/', r'/support/', r'/services/',
            r'/category/', r'/tag/', r'/search\b',
            r'/apply/?$', r'/how-to-apply/?$',
            r'/scholarships?/?$',
            r'/fees?/?$',
            r'/entry-requirements/?$',
            r'/info-night',
            r'/open-day',
        ]
        
        # Valid course URL patterns
        self.valid_course_url_patterns = [
            r'/courses?/[a-z0-9-]+/?$',
            r'/study/[a-z0-9-]+/?$',
            r'/programs?/[a-z0-9-]+/?$',
            r'/degrees?/[a-z0-9-]+/?$',
            r'/[a-z]+-courses?/[a-z0-9-]+/?$',
        ]
    
    
    async def validate_samples(
        self, 
        sample_urls: List[str],
        country: str
    ) -> Dict:
        """
        Validate sample URLs and learn patterns
        """
        
        results = []
        
        # Validate each sample
        for url in sample_urls:
            result = await self._validate_single_url(url)
            results.append(result)
        
        # Separate valid and invalid
        valid = [r for r in results if r['is_valid']]
        invalid = [r for r in results if not r['is_valid']]
        
        # Learn patterns from valid samples
        learned_patterns = self._learn_patterns(valid, invalid)
        
        return {
            'total_samples': len(results),
            'valid_samples': len(valid),
            'invalid_samples': len(invalid),
            'success_rate': len(valid) / len(results) if results else 0,
            'valid_examples': [
                {
                    'url': v['url'],
                    'title': v['title'],
                    'score': v['score'],
                    'reason': v['reason']
                }
                for v in valid[:5]
            ],
            'invalid_examples': [
                {
                    'url': inv['url'],
                    'title': inv['title'],
                    'score': inv['score'],
                    'reason': inv['reason']
                }
                for inv in invalid[:5]
            ],
            'learned_patterns': learned_patterns
        }
    
    
    async def _validate_single_url(self, url: str) -> Dict:
        """
        Validate a single URL with multi-factor scoring
        
        UPDATED SCORING:
        - URL structure: 0.0 - 0.35
        - Title keywords: -1.0 - 0.40 (can be negative for junk)
        - Content signals: 0.0 - 0.25
        
        Accept threshold: 0.40 (lowered from 0.50)
        
        SPECIAL CASE: Perfect URL + degree keyword = auto-accept
        """
        
        try:
            # Fetch page
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=15) as response:
                    html = await response.text()
            
            # Extract title
            soup = BeautifulSoup(html, 'html.parser')
            title_tag = soup.find('title') or soup.find('h1')
            title = title_tag.get_text(strip=True) if title_tag else 'No title'
            
            # Score URL
            url_score = self._score_url(url)
            
            # Score title (can be negative for junk)
            title_score = self._score_title(title)
            
            # Score content
            content_score = self._score_content(html)
            
            # Total score
            total_score = url_score + title_score + content_score
            
            # Determine validity
            is_valid = total_score >= 0.40  # LOWERED from 0.50
            
            # SPECIAL CASE: Perfect URL + degree keyword = auto-accept
            if url_score >= 0.30 and title_score >= 0.40:
                is_valid = True
                total_score = max(total_score, 0.60)  # Boost score
            
            # REJECT if junk detected (negative title score)
            if title_score < 0:
                is_valid = False
            
            # Generate reason
            reason = self._generate_reason(
                url, title, url_score, title_score, content_score, is_valid
            )
            
            return {
                'url': url,
                'title': title,
                'is_valid': is_valid,
                'score': round(total_score, 2),
                'url_score': round(url_score, 2),
                'title_score': round(title_score, 2),
                'content_score': round(content_score, 2),
                'reason': reason
            }
            
        except Exception as e:
            return {
                'url': url,
                'title': 'Error fetching',
                'is_valid': False,
                'score': 0.0,
                'url_score': 0.0,
                'title_score': 0.0,
                'content_score': 0.0,
                'reason': f'Fetch error: {str(e)}'
            }
    
    
    def _score_url(self, url: str) -> float:
        """
        Score URL structure (0.0 - 0.35)
        """
        
        score = 0.0
        url_lower = url.lower()
        
        # Check valid patterns
        for pattern in self.valid_course_url_patterns:
            if re.search(pattern, url_lower, re.I):
                score += 0.30
                break
        
        # Check for degree keywords in URL slug
        url_path = urlparse(url).path
        for keyword in self.degree_keywords:
            if re.search(keyword, url_path, re.I):
                score += 0.15
                break
        
        # Check invalid patterns (strong negative)
        for pattern in self.invalid_url_patterns:
            if re.search(pattern, url_lower, re.I):
                score -= 0.50
                break
        
        return max(0.0, min(0.35, score))
    
    
    def _score_title(self, title: str) -> float:
        """
        Score title (-1.0 - 0.40)
        
        CRITICAL: Can return negative scores for junk pages
        """
        
        score = 0.0
        title_lower = title.lower()
        
        # ─────────────────────────────────────────────────────
        # STEP 1: Check for explicit junk patterns (REJECT)
        # ─────────────────────────────────────────────────────
        
        for pattern in self.junk_patterns:
            if re.search(pattern, title_lower):
                return -1.0  # STRONG NEGATIVE - auto-reject
        
        # ─────────────────────────────────────────────────────
        # STEP 2: Check for degree keywords (ACCEPT)
        # ─────────────────────────────────────────────────────
        
        for keyword in self.degree_keywords:
            if re.search(keyword, title_lower, re.I):
                score += 0.40
                break
        
        # ─────────────────────────────────────────────────────
        # STEP 3: Check for non-course signals (PENALIZE)
        # ─────────────────────────────────────────────────────
        
        for signal in self.non_course_signals:
            if re.search(signal, title_lower, re.I):
                score -= 0.30
                break
        
        return max(-1.0, min(0.40, score))
    
    
    def _score_content(self, html: str) -> float:
        """
        Score content signals (0.0 - 0.25)
        """
        
        score = 0.0
        text = re.sub(r'<[^>]+>', ' ', html).lower()
        
        # Count course signals
        signal_count = 0
        for signal in self.course_signals:
            if re.search(signal, text, re.I):
                signal_count += 1
                if signal_count >= 5:
                    break
        
        score += signal_count * 0.05
        
        # Check for non-course signals
        for signal in self.non_course_signals:
            if re.search(signal, text, re.I):
                score -= 0.15
                break
        
        return max(0.0, min(0.25, score))
    
    
    def _generate_reason(
        self, 
        url: str, 
        title: str,
        url_score: float,
        title_score: float,
        content_score: float,
        is_valid: bool
    ) -> str:
        """
        Generate human-readable reason
        """
        
        if is_valid:
            reasons = []
            if url_score > 0:
                reasons.append(f'valid URL structure (+{url_score:.2f})')
            if title_score > 0:
                reasons.append(f'degree keywords in title (+{title_score:.2f})')
            if content_score > 0:
                reasons.append(f'course content signals (+{content_score:.2f})')
            
            return f"Valid course: {', '.join(reasons)}"
        else:
            if title_score < 0:
                return f"Junk page detected: {title}"
            
            reasons = []
            if url_score <= 0:
                reasons.append('URL pattern does not match course pages')
            if title_score <= 0:
                reasons.append('no degree keywords in title')
            if content_score <= 0:
                reasons.append('missing course content signals')
            
            return f"Not a course: {', '.join(reasons)}"
    
    
    def _learn_patterns(self, valid: List[Dict], invalid: List[Dict]) -> Dict:
        """
        Learn patterns from validated samples
        """
        
        learned = {
            'valid_url_patterns': [],
            'valid_title_keywords': [],
            'invalid_url_patterns': [],
            'invalid_title_keywords': [],
            'confidence': 0.0
        }
        
        # Extract URL patterns from valid samples
        for v in valid:
            url = v['url']
            path = urlparse(url).path
            
            match = re.search(r'(/[^/]+/)[a-z0-9-]+/?$', path, re.I)
            if match:
                pattern = match.group(1) + '[a-z0-9-]+'
                if pattern not in learned['valid_url_patterns']:
                    learned['valid_url_patterns'].append(pattern)
        
        # Extract title keywords from valid samples
        for v in valid:
            title = v['title'].lower()
            for keyword in self.degree_keywords:
                if re.search(keyword, title, re.I):
                    kw_text = re.search(keyword, title, re.I).group(0)
                    if kw_text not in learned['valid_title_keywords']:
                        learned['valid_title_keywords'].append(kw_text)
        
        # Extract invalid patterns
        for inv in invalid:
            url = inv['url']
            
            for pattern in self.invalid_url_patterns:
                if re.search(pattern, url, re.I):
                    if pattern not in learned['invalid_url_patterns']:
                        learned['invalid_url_patterns'].append(pattern)
        
        # Calculate confidence
        total = len(valid) + len(invalid)
        learned['confidence'] = len(valid) / total if total > 0 else 0
        
        return learned
    
    
    def filter_urls_by_patterns(
        self, 
        urls: List[str], 
        patterns: Dict
    ) -> List[str]:
        """
        Filter URLs using learned patterns
        """
        
        filtered = []
        
        for url in urls:
            # Check if URL matches valid patterns
            matches_valid = False
            
            for pattern in patterns.get('valid_url_patterns', []):
                if re.search(pattern, url, re.I):
                    matches_valid = True
                    break
            
            # Check if URL matches invalid patterns
            matches_invalid = False
            
            for pattern in patterns.get('invalid_url_patterns', []):
                if re.search(pattern, url, re.I):
                    matches_invalid = True
                    break
            
            # Also check against known invalid patterns
            for pattern in self.invalid_url_patterns:
                if re.search(pattern, url, re.I):
                    matches_invalid = True
                    break
            
            # Accept if matches valid and doesn't match invalid
            if matches_valid and not matches_invalid:
                filtered.append(url)
            
            # OR accept if URL structure looks like a course page
            elif not matches_invalid:
                for pattern in self.valid_course_url_patterns:
                    if re.search(pattern, url, re.I):
                        filtered.append(url)
                        break
        
        return filtered
