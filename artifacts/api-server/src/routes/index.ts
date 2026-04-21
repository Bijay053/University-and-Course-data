import { Router, type IRouter } from "express";
import healthRouter from "./health";
import universitiesRouter from "./universities";
import coursesRouter from "./courses";
import intakesRouter from "./intakes";
import feesRouter from "./fees";
import englishRequirementsRouter from "./english_requirements";
import academicRequirementsRouter from "./academic_requirements";
import scholarshipsRouter from "./scholarships";
import scrapingRouter from "./scraping";
import dashboardRouter from "./dashboard";
import bulkRouter from "./bulk";
import importRouter from "./import";
import scrapeRouter from "./scrape";
import backupRouter from "./backup";
import backupMappingRouter from "./backup_mapping";

const router: IRouter = Router();

router.use(healthRouter);
router.use(universitiesRouter);
router.use(coursesRouter);
router.use(intakesRouter);
router.use(feesRouter);
router.use(englishRequirementsRouter);
router.use(academicRequirementsRouter);
router.use(scholarshipsRouter);
router.use(scrapingRouter);
router.use(dashboardRouter);
router.use(bulkRouter);
router.use(importRouter);
router.use(scrapeRouter);
router.use(backupRouter);
router.use(backupMappingRouter);

export default router;
