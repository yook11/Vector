import { z } from "zod";

// CSPが禁止するevalの能力検査を、各schemaの構築前に止める。
z.config({ jitless: true });
