import "@fontsource/barlow/400.css";
import "@fontsource/barlow/400-italic.css";
import "@fontsource/barlow/500.css";
import "@fontsource/barlow/600.css";
import "@fontsource/barlow/700.css";
import "./styles/base.css";
import "./styles/ask.css";
import "./styles/pipeline.css";
import "./styles/split.css";
import "./styles/rail.css";
import "./styles/source.css";
import { createRoot } from "react-dom/client";
import App from "./App";

createRoot(document.getElementById("root") as HTMLElement).render(<App />);
