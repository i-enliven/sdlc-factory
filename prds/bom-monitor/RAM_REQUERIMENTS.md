# Product Requirements Document: bom-monitor

## 1. Project Overview
**bom-monitor** is a centralized lifecycle governance platform. It tracks the "health" of software projects by cross-referencing CycloneDX `bom.xml` files with the **endoflife.date** API. It provides a modern dashboard featuring a global leaderboard, historical project tracking, and automated, status-change-driven alerts.

---

## 2. Functional Requirements

### 2.1 The Global Dashboard (Entry Point)
* **FR-1:** **Leaderboard:** The root URL (`/`) MUST display a "Global Health Leaderboard" ranking all projects by their current Health Score ($S_p$).
* **FR-2:** **Organization Metrics:** The dashboard header MUST display:
    * Total Projects Monitored.
    * Total Components across all projects.
    * Fleet-wide count of **CRITICAL** and **URGENT** flags.

### 2.2 Project Ingestion & Repository Mapping
* **FR-3:** **Submission Form:** Users MUST provide a **Project Name**, **Repository URL**, and **Contact Email** alongside the `bom.xml` file.
* **FR-4:** **Persistence:** Every upload MUST be stored in the database. The system tracks version history, but the dashboard reflects the state of the *most recent* upload for any given Project ID.
* **FR-5:** **API Ingestion:** Provide a `POST /api/v1/ingest` endpoint for CI/CD pipelines.

### 2.3 Lifecycle Logic & Flagging
The system MUST calculate the time remaining until end-of-life ($\Delta T$):
$$\Delta T = \text{EOL\_Date} - \text{Current\_Date}$$

| Status | Condition ($\Delta T$ in days) | Hex Color | Weight ($W$) |
| :--- | :--- | :--- | :--- |
| **CRITICAL** | $\Delta T \le 0$ | `#FF0000` | 20 |
| **URGENT** | $0 < \Delta T \le 90$ | `#FFA500` | 10 |
| **WARNING** | $91 < \Delta T \le 180$ | `#FFFF00` | 5 |
| **NOTICE** | $181 < \Delta T \le 365$ | `#007BFF` | 2 |
| **HEALTHY** | $\Delta T > 365$ / Active | `#008000` | 0 |

---

## 3. Scoring & Translation Engine

### 3.1 Health Score Formulas
**Individual Project Score ($S_p$):**
$$S_p = \max\left(0, 100 - \sum (N_{status} \times W_{status})\right)$$
*(Where $N$ is the count of components per status and $W$ is the assigned weight.)*

**Global Health Index ($G$):**
$$G = \frac{\sum S_p}{\text{Total Projects}}$$

### 3.2 The Mapping Engine
To handle naming discrepancies (e.g., `nodejs` vs `node`):
1. **Alias Check:** Query the `Aliases` table for a `bom_name` match.
2. **API Query:** If no alias, query `https://endoflife.date/api/{product}.json`.
3. **Fallback:** If the API returns a 404, mark the component as **UNKNOWN** (Grey: `#808080`) and allow manual mapping in the UI to update the `Aliases` table.

### 3.3 Parsing & Schema Robustness
* **FR-23: Multi-Version Support:** The parser MUST support CycloneDX XML schema versions **1.4, 1.5, and 1.6**. It should not hard-code validation against a single version.
* **FR-24: Namespace Agnostic Parsing:** The XML logic MUST handle or strip XML namespaces (e.g., `xmlns="http://cyclonedx.org/schema/bom/1.5"`) to prevent "Element Not Found" errors during XPath queries or object mapping.
* **FR-25: Encoding Resilience:** The system MUST explicitly handle **UTF-8** and **UTF-16** encodings and correctly strip Byte Order Marks (BOM) before parsing.
* **FR-26: Namespace-Agnostic Querying:** The parser MUST use wildcard namespace handling (e.g., `{*}component`) or strip namespaces entirely before processing. It MUST be compatible with the output of the `cyclonedx-bom` Python library.
* **FR-27: Metadata Extraction:** The system MUST specifically look for the `metadata -> component` section to identify the root application, but use the `components -> component` list for the dependency audit.
* **FR-28: Schema Fallback:** If the `bom.xml` does not specify a version in the root tag, the parser MUST default to **CycloneDX 1.5** logic rather than throwing an "Invalid" error.
* **FR-29: Rate Limit Handling:** The EndOfLifeClient MUST implement a small delay (e.g., 100ms) or a retry-on-429 logic when querying the endoflife.date API for BOMs containing more than 50 components to avoid IP throttling.
---

## 4. Technical Architecture

### 4.1 Backend Standards
* **Language:** Python 3.12+ using **uv** for dependency management.
* **Framework:** **FastAPI** (Asynchronous).
* **Database:** **PostgreSQL** (Managed via SQLAlchemy or Tortoise).
* **Authentication:** `X-API-KEY` header for all `POST/PUT/DELETE` operations.
* **Documentation:** Swagger UI enabled at `/docs`.
* **FR-30: Unified API Envelope:** Every collection endpoint MUST return a consistent JSON object. The `/api/v1/projects` endpoint MUST return a `GlobalLeaderboard` object: `{ "projects": [...], "global_metrics": {...} }`.
* **FR-31: Score Precision:** Health scores ($S_p$) MUST be calculated as floats but rounded to **one decimal place** (e.g., `85.4`) before being sent to the UI to maintain clean "modern" visuals.

### 4.2 Frontend Standards
The interface MUST moderns and rich.
* **Theme:** **Glassmorphism / Adaptive Dark Mode.** Use semi-transparent backgrounds with background-blur filters (`backdrop-filter: blur(10px)`).
* **Design System:** Use **Tailwind CSS** or **NextUI**.
* **Visuals:**
    * **Status Badges:** Use glowing neon accents for flags (e.g., a pulsing red glow for "Critical").
    * **Lifecycle Timeline:** A horizontal SVG "health bar" for each project showing how many dependencies fall into each color bucket.
    * **Interactivity:** Framer Motion for smooth row expansions to show "Recommended Upgrade Path."
* **FR-32: Testability (Data-TestIDs):** Interactive elements and data cells MUST include `data-testid` attributes (e.g., `data-testid="project-score"`, `data-testid="alias-mapping-row"`).
* **FR-33: Exact Text Matching:** Downstream agents (Testers/Coders) MUST use exact matching or `data-testid` for E2E selectors to avoid collisions between similar strings (e.g., "node" vs "nodejs").

### 4.3 Deployment & Networking
The application MUST be deployed via **Docker Compose**:
* **Network Isolation:** All containers reside on a private `bom-net`.
* **Database Security:** The PostgreSQL container MUST NOT expose any ports to the host.
* **Gateway:** Only the FastAPI/UI service exposes ports (`80/443`) to the host.
* **FR-34: Port Mapping Persistence:** In the `docker-compose.yml`, the host port mapping for PostgreSQL SHOULD BE omitted entirely. If local DB testing is required, use host port **5433** to map to container port **5432** to avoid collisions with existing host services.
---

## 5. Automation & Alerts

### 5.1 Automated Daily Sweep
* **Trigger:** Daily CRON job (00:00 UTC).
* **Action:** Re-fetch API data for the latest BOM of every project. Update component statuses in the DB.
* **Trend Analysis:** Calculate the delta between $S_p(today)$ and $S_p(yesterday)$.

### 5.2 Consolidated Alerting
* **Provider:** SendGrid (Mocked for initial build).
* **Rule:** If a component status moves to a higher-weight tier (e.g., Warning $\rightarrow$ Urgent), trigger an alert.
* **Consolidation:** Send exactly **one** email per project per day containing all lifecycle changes.

---

## 6. Implementation Path - Take into account the following suggestions

### 6.1 (Agent Step-by-Step)
* Initialize environment using `uv init` and `pyproject.toml`.
* Configure Docker Compose with internal networking and volume persistence.
* Define PostgreSQL schema (Projects, Uploads, Components, Aliases, Alert_History).
* Implement `CycloneDX` XML parser to JSON.
    * **Pre-Validation:** Check if the file size is $>0$ and starts with a valid `<bom` tag.
    * **Schema-Agnostic Search:** Use an XML library (like `lxml` in Python) that allows for `local-name()` searches to find `<component>` tags regardless of the specific CycloneDX namespace used.
    * **Data Extraction:** If the parser fails, catch the exception and return the **raw parser error string** to the client for immediate debugging.
* Build the FastAPI service with the $\Delta T$ and $S_p$ logic.
* Build the Dashboard (Global Score Index + Project Details).

#### Notes
**Agent Note:** When implementing the `CycloneDXParser` in Python (FastAPI), use `lxml` with the following strategy to avoid the "Invalid/Empty" bug:
* **1. **Capture Namespaces:** Extract the `xmlns` from the root `<bom>` element.
* **2. **Wildcard Search:** Instead of searching for `bom:component`, search for `//{*}component`. This ensures that even if the library changes the URI (e.g., from version 1.4 to 1.5), the components are still found.
* **3. **Validation Log:** Before returning a `400` error, log the first 500 characters of the received XML to the internal console to verify the stream was received correctly.
**Agent Note (Testing):** When testing Next.js pages, you MUST mock `next/router` in `jest.setup.ts`. The implementation of `ProjectDetailsPage` MUST handle both `router.query` and direct `id` props to ensure testability outside the full Next.js lifecycle.
**Agent Note:** When calculating $S_p$, ensure the weight for **UNKNOWN** status is **0**, as an unmapped component shouldn't penalize a project's health until a human or agent manually maps it.

### 6.2 Enhanced Error Payloads
* **EC-4: Verbose Validation Errors:** In the event of a `400 Bad Request`, the API MUST return a structured JSON body containing the specific line number and reason for the validation failure (e.g., `{"error": "Missing mandatory field: metadata.component", "line": 12}`). A generic "Invalid XML" response is strictly prohibited.
* **EC-5: Empty Buffer Check:** The ingestion logic MUST verify that the file stream is fully read and not empty before passing it to the parser to avoid "Empty File" false positives.
---

**Final Logic Check:**
Does the agent have the formula for the Health Score? **Yes.**
Is the tech stack locked down? **Yes.**
Is the UI style defined? **Yes.**
Is the database inaccessible from the host? **Yes.**

# IMPORTANT
**Validation**: Included with the project is a bom.xml file that can be used to test the parser. 
    * It contains 210 components which should be reflected after importing the bom.xml file.
