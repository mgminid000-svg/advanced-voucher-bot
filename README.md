# Advanced Voucher Bot

This is an advanced Telegram bot designed to check voucher codes with enhanced features like ML-powered CAPTCHA solving, proxy rotation, and session pooling.

## Deployment to Railway

Follow these steps to deploy the bot to Railway:

1.  **Sign up for Railway:** If you don't have an account, sign up at [Railway.app](https://railway.app/).

2.  **Create a new project:**
    *   Click on "New Project" in your Railway dashboard.
    *   Select "Deploy from GitHub Repo" and connect your GitHub account.
    *   Choose the `advanced-voucher-bot` repository (which will be created in the next steps).

3.  **Configure Environment Variables:**
    *   In your Railway project settings, go to the "Variables" tab.
    *   Add the following environment variables:
        *   `BOT_TOKEN`: Your Telegram bot token, obtained from BotFather.
        *   `ADMIN_ID`: Your Telegram user ID, which will be the admin of the bot.
        *   `PORT`: The port for the web server (default is `8099`). Railway will automatically expose this.
        *   `CONCURRENCY`: (Optional) Number of concurrent tasks for brute-forcing (default is `10`).
        *   `PROXIES`: (Optional) Comma-separated list of proxies (e.g., `http://user:pass@ip:port,http://user2:pass2@ip2:port2`).

4.  **Deployment:** Railway will automatically detect the `Dockerfile` and `railway.toml` and deploy your bot. The bot will start running and be accessible via Telegram.

## Local Development

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/<your-username>/advanced-voucher-bot.git
    cd advanced-voucher-bot
    ```

2.  **Create a virtual environment and install dependencies:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Set up environment variables:**
    Create a `.env` file in the root directory with your bot token and admin ID:
    ```
    BOT_TOKEN=YOUR_BOT_TOKEN
    ADMIN_ID=YOUR_ADMIN_ID
    PORT=8099
    ```

4.  **Run the bot:**
    ```bash
    python3 bot.py
    ```

## Bot Commands

*   `/start`: Displays welcome message and available commands.
*   `/setup`: Sets up the session URL for voucher checking.
*   `/brute`: Starts the brute force process for voucher codes.
*   `/stop`: Stops the current brute force process.
*   `/status`: Shows the current status of the bot.
*   `/saved`: Displays all found and saved voucher codes.
*   `/stats`: Shows performance statistics of the bot.
