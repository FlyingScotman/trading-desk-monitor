import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ScenarioLab } from './screens/ScenarioLab';
import './styles.css';

createRoot(document.getElementById('root')!).render(<StrictMode><ScenarioLab /></StrictMode>);
