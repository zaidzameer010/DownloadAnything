import { ContextMenu } from "./components/ContextMenu";
import { Dashboard } from "./components/Dashboard";
import { FormatDrawer } from "./components/FormatDrawer";
import { Modals } from "./components/Modals";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { useDownloader } from "./hooks/useDownloader";
import "./App.css";

function App() {
	const downloader = useDownloader();

	return (
		<div className="dashboard-container">
			<Sidebar downloader={downloader} />

			<main className="main-area">
				{downloader.activeTab === "downloads" && (
					<Dashboard downloader={downloader} />
				)}
				{downloader.activeTab === "settings" && (
					<SettingsPanel downloader={downloader} />
				)}
			</main>

			<FormatDrawer downloader={downloader} />
			<ContextMenu downloader={downloader} />
			<Modals downloader={downloader} />
		</div>
	);
}

export default App;
