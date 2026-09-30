import { createApp } from 'vue'
import router from './router'
import './styles/prototype/tokens.css'
import './styles/prototype/base.css'
import './styles/prototype/app.css'
import './styles/prototype/landing.css'
import './styles/prototype/auth.css'
import './styles/workspace.css'
import './style.css'
import App from './App.vue'

createApp(App).use(router).mount('#app')
