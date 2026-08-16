/**
 * Entry point. registerRootComponent handles both the web bundle and a native
 * build, so App.js never needs to know which it is running under.
 */
import { registerRootComponent } from 'expo';
import App from './App';

registerRootComponent(App);
