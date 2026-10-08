// Where the Android app finds the Sahayam server.
// - Same Wi-Fi demo: your laptop's IP address, e.g. "http://192.168.1.20:8000"
//   (find it with `ipconfig` on Windows or `ip addr` / `ifconfig` on Linux/macOS)
// - Hosted server: "https://your-sahayam-server.example.org"
// Leave it empty ("") to run the app in demo mode with sample data.
// You can also change it inside the app: Sign in screen -> Server.
// After editing, run `npm run build` in web/ and `npx cap sync android` in mobile/.
window.SAHAYAM_CONFIG = { apiBase: "" };
