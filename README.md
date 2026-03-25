# SteamCICD

Automated pipeline for downloading build artifacts from Unity Cloud Build and uploading them to Steam via SteamCMD.

## Overview

SteamCICD acts as a bridge between Unity DevOps (Cloud Build) and Steamworks. It continuously polls the Unity Cloud Build API for new successful builds, downloads the artifacts, extracts them, and uses SteamCMD to push them to your Steam depots.

The project includes a web interface for initial setup (SDK upload, Steam login) and a background poller for continuous operation.

## Features

- **Automated Polling:** Monitors Unity Cloud Build for new successful builds.
- **Artifact Management:** Downloads and tracks which builds have been processed.
- **Steam Integration:** Automates SteamCMD for uploading builds to Steam.
- **Web Interface:** Easy setup for Steamworks SDK and Steam authentication.
- **Dockerized:** Ready to be deployed as a container.

## Tech Stack

- **Language:** Python 3.14+
- **Framework:** Flask (Web UI & API)
- **Dependencies:** `requests`, `python-dotenv`, `werkzeug`
- **Infrastructure:** Docker, Docker Compose, GitHub Actions

## Requirements

- Unity Cloud Build API Key, Organization ID, and Project ID.
- Steam App ID and Depot ID(s).
- Steamworks SDK (uploaded via the Web UI).
- Steam account with permissions to upload builds.

## Project Structure

```text
.
├── app.py              # Flask application routes
├── main.py             # Application entry point (starts poller & web server)
├── poller.py           # Background task for Unity Cloud Build polling
├── unity_client.py     # Unity Cloud Build API client
├── uploader.py         # SteamCMD wrapper and VDF generation
├── artifact_manager.py # Local tracking of downloaded/uploaded builds
├── config.py           # Environment configuration
├── Dockerfile          # Container definition
├── docker-compose.yml  # Docker Compose orchestration
├── requirements.txt    # Python dependencies
├── downloads/          # (Created at runtime) Artifact storage
└── steamworks_sdk/     # (Created at runtime) Extracted Steamworks SDK
```

## Setup & Installation

### Local Setup

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd SteamCICD
   ```

2. **Create a `.env` file:**
   Copy the example below and fill in your details.
   ```bash
   UNITY_API_KEY=your_unity_api_key
   UNITY_ORG_ID=your_org_id
   UNITY_PROJECT_ID=your_project_id
   # Optional:
   # POLL_INTERVAL_SECONDS=60
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the application:**
   ```bash
   python main.py
   ```
   Access the Web UI at `http://localhost:5000`.

### Docker Setup

1. **Build and start with Docker Compose:**
   ```bash
   docker-compose up -d --build
   ```

2. **Access the Web UI:**
   Navigate to `http://localhost:5000` to complete the initial setup.

## Initial Configuration (Web UI)

1. **Upload Steamworks SDK:** Use the dashboard to upload the `steamworks_sdk.zip` file. This is required to provide the `steamcmd` binaries.
2. **Steam Login:** Perform a Steam login through the Web UI. If Steam Guard is enabled, you will be prompted for the code.
3. **Automatic Polling:** Once configured, the background poller will automatically start processing builds.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `UNITY_API_KEY` | Unity Cloud Build API Key | (Required) |
| `UNITY_ORG_ID` | Unity Organization ID | (Required) |
| `UNITY_PROJECT_ID` | Unity Project ID | (Required) |
| `UNITY_API_BASE_URL` | Base URL for Unity API | `https://build-api.cloud.unity3d.com/api/v1` |
| `POLL_INTERVAL_SECONDS`| Frequency of polling Unity API | `60` |
| `DOWNLOAD_DIR` | Path to store downloaded builds | `./downloads` |
| `STEAMWORKS_ROOT` | Path to Steamworks SDK | `./steamworks_sdk` |

## Scripts & Entry Points

- `main.py`: The primary entry point. Validates settings, starts the background poller thread, and launches the Flask web server.
- `app.py`: Defines the web interface and API endpoints.
- `poller.py`: Contains the logic for fetching build lists and initiating downloads.

## Tests

- TODO: Add automated tests for API clients and artifact processing.

## License

- TODO: Specify license (e.g., MIT, Proprietary).
