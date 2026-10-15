import { defineConfig, devices } from "@playwright/test";
if (process.env.KNOWFLOW_M53_LIVE !== "1" || !process.env.KNOWFLOW_M53_RUN_ROOT)
  throw new Error("Run only through the isolated live test harness");
export default defineConfig({
  testDir:"./tests/live", timeout:900000, expect:{timeout:15000},workers:1,maxFailures:1,
  outputDir:"test-results/m53-live", fullyParallel:false,
  use:{baseURL:"http://127.0.0.1:3109",trace:"off",screenshot:"off",
    launchOptions:{executablePath:process.env.KNOWFLOW_BROWSER ?? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"}},
  projects:[{name:"desktop",use:{...devices["Desktop Chrome"]}},
    {name:"mobile",use:{viewport:{width:390,height:844},isMobile:true,hasTouch:true}}],
  webServer:{command:"npm run dev -- --port 3109",url:"http://127.0.0.1:3109/login",reuseExistingServer:false,timeout:180000,
    env:{KNOWFLOW_API_URL:"http://127.0.0.1:8109",KNOWFLOW_WEB_ORIGIN:"http://127.0.0.1:3109",NEXT_TELEMETRY_DISABLED:"1"}},
});
