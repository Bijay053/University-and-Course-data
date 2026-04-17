"""
Orchestrator Updates - Production Ready
Add these methods to your existing orchestrator.py file
"""

# ADD THIS METHOD TO ScraperOrchestrator CLASS

async def start_scrape(self, university_id: int, url: str, job_id: str) -> Dict:
    """
    UPDATED: Better validation failure handling
    """
    
    job = await ScrapeJob.get(job_id)
    university = await University.get(university_id)
    
    try:
        # ... (Phase 1 - URL Analysis - keep existing code)
        
        # ═══════════════════════════════════════════════════════
        # PHASE 2: RESEARCH & VALIDATION (UPDATED)
        # ═══════════════════════════════════════════════════════
        
        await self._update_job_status(job, 'researching', {
            'message': 'Sampling course pages for validation...'
        })
        
        sample_urls = await self.course_discovery.get_sample_urls(
            url, 
            url_analysis,
            limit=12  # Increased sample size
        )
        
        validation_results = await self.research_validator.validate_samples(
            sample_urls,
            university.country
        )
        
        # Log results
        print(f"[INFO] Validation complete:")
        print(f"  - Valid: {validation_results['valid_samples']}/{validation_results['total_samples']}")
        print(f"  - Success rate: {validation_results['success_rate']:.0%}")
        
        # ═══════════════════════════════════════════════════════
        # CRITICAL: Handle complete validation failure
        # ═══════════════════════════════════════════════════════
        
        if validation_results['success_rate'] == 0.0:
            await self._update_job_status(job, 'failed', {
                'message': 'CRITICAL: All sample pages rejected. Validation may be misconfigured.',
                'details': validation_results,
                'action_required': 'Manual review needed'
            })
            return {
                'status': 'failed',
                'reason': 'complete_validation_failure',
                'details': validation_results
            }
        
        # Warn if low success rate but allow continuation
        if validation_results['success_rate'] < 0.50:
            await self._update_job_status(job, 'warning', {
                'message': f'Low validation success rate: {validation_results["success_rate"]:.0%}',
                'details': validation_results,
                'proceeding': 'Using URL filtering as fallback'
            })
        
        # ... (rest of existing code - Phase 3 onwards)


# ADD THIS METHOD TO ScraperOrchestrator CLASS

async def continue_after_approval(self, job_id: str) -> Dict:
    """
    UPDATED: Add junk filtering before staging
    """
    
    job = await ScrapeJob.get(job_id)
    university = await University.get(job.university_id)
    
    filtered_urls = job.progress_data.get('filtered_urls', [])
    saved_patterns = await self.pattern_storage.get_patterns(university.id)
    
    extracted_courses = []
    errors = []
    
    # ═══════════════════════════════════════════════════════
    # PHASE 4: EXTRACTION (keep existing code)
    # ═══════════════════════════════════════════════════════
    
    for idx, course_url in enumerate(filtered_urls):
        try:
            await self._update_job_status(job, 'extracting', {
                'message': f'Extracting course {idx + 1} of {len(filtered_urls)}',
                'current_url': course_url,
                'progress': (idx + 1) / len(filtered_urls)
            })
            
            course_data = await self._extract_complete_course(
                course_url,
                university,
                saved_patterns
            )
            
            extracted_courses.append(course_data)
            await asyncio.sleep(0.5)
            
        except Exception as e:
            errors.append({'url': course_url, 'error': str(e)})
            continue
    
    # ═══════════════════════════════════════════════════════
    # PHASE 5: VALIDATION & JUNK FILTERING (NEW)
    # ═══════════════════════════════════════════════════════
    
    await self._update_job_status(job, 'validating', {
        'message': 'Validating and filtering junk pages...'
    })
    
    validated_courses = []
    junk_filtered = []
    
    for course in extracted_courses:
        # Check if junk page
        if self._is_junk_page(course):
            junk_filtered.append({
                'name': course.get('course_name'),
                'reason': 'Non-course page (event/category/news)'
            })
            continue
        
        # Validate data quality
        validation = self.validator.validate_course(course)
        
        # Reject if too incomplete
        if validation['score'] < 0.40:
            junk_filtered.append({
                'name': course.get('course_name'),
                'reason': f'Low quality: {validation["score"]:.0%}',
                'missing': validation['missing_fields']
            })
            continue
        
        course['_validation'] = validation
        validated_courses.append(course)
    
    print(f"[INFO] Filtered out {len(junk_filtered)} junk/low-quality pages")
    for junk in junk_filtered[:10]:  # Log first 10
        print(f"  - {junk['name']}: {junk['reason']}")
    
    # ═══════════════════════════════════════════════════════
    # PHASE 6: STAGING (keep existing code but use validated_courses)
    # ═══════════════════════════════════════════════════════
    
    staged_ids = []
    for course in validated_courses:
        staged = await StagedCourse.create(
            job_id=job_id,
            university_id=university.id,
            data=course,
            validation_score=course['_validation']['score'],
            missing_fields=course['_validation']['missing_fields']
        )
        staged_ids.append(staged.id)
    
    # ... (rest of existing code)
    
    return {
        'status': 'success',
        'job_id': job_id,
        'courses_extracted': len(extracted_courses),
        'courses_staged': len(staged_ids),
        'courses_filtered': len(junk_filtered),
        'errors': errors[:10]
    }


# ADD THIS METHOD TO ScraperOrchestrator CLASS

def _is_junk_page(self, course_data: Dict) -> bool:
    """
    Detect junk pages that shouldn't be staged
    
    Returns True if this is NOT a real course
    """
    
    course_name = course_data.get('course_name', '').lower()
    
    junk_patterns = [
        r'\binfo\s+night\b',
        r'\bvirtual\s+info\s+night\b',
        r'\bopen\s+day\b',
        r'^double\s+degrees?$',
        r'^graduate\s+certificates?$',
        r'^undergraduate\s+courses?$',
        r'^postgraduate\s+courses?$',
        r'\bretains?\s+tier\b',
        r'\brankings?\b',
        r'\baccredited\b$',
        r'\bwhy\s+choose\b',
        r'\bapply\s+now\b$',
    ]
    
    for pattern in junk_patterns:
        if re.search(pattern, course_name):
            return True
    
    # Check if missing ALL required fields
    required = ['degree_level', 'duration', 'international_fee']
    missing_count = sum(1 for field in required if not course_data.get(field))
    
    if missing_count == len(required):
        return True
    
    return False


# ADD THIS METHOD TO ScraperOrchestrator CLASS

async def _extract_complete_course(
    self,
    course_url: str,
    university: University,
    saved_patterns: Optional[Dict] = None
) -> Dict:
    """
    UPDATED: Pass university requirements URL to extractors
    """
    
    # Fetch course page
    html = await self._fetch_html(course_url)
    
    course_data = {
        'course_url': course_url,
        'university_name': university.name
    }
    
    # Extract with all extractors
    for name, extractor in self.extractors.items():
        try:
            if name == 'english':
                # UPDATED: Pass university requirements URL
                result = await extractor.extract(
                    html, 
                    course_url, 
                    university.country,
                    university_requirements_url=getattr(university, 'requirements_url', None)
                )
            else:
                result = await extractor.extract(html, course_url, university.country)
            
            course_data.update(result)
        except Exception as e:
            print(f"[WARNING] Extractor {name} failed: {e}")
    
    # Multi-page extraction (keep existing logic)
    linked_pages = self._discover_linked_pages(html, course_url)
    
    # ... (rest of existing multi-page extraction code)
    
    return course_data
