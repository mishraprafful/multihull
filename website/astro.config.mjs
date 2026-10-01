import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://multihull.dev',
  integrations: [
    starlight({
      title: 'Multihull',
      description: 'Deploy hot GPU containers to many providers. One URL. Automatic failover.',
      logo: { src: './src/assets/mark.svg', alt: 'Multihull' },
      favicon: '/favicon.svg',
      customCss: ['./src/styles/theme.css'],
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/mishraprafful/multihull' },
      ],
      head: [
        {
          tag: 'script',
          content:
            "try{if(!localStorage.getItem('starlight-theme'))localStorage.setItem('starlight-theme','dark')}catch(e){}",
        },
        {
          tag: 'link',
          attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' },
        },
        {
          tag: 'link',
          attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: true },
        },
        {
          tag: 'link',
          attrs: {
            rel: 'stylesheet',
            href: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap',
          },
        },
      ],
      sidebar: [
        { label: 'Quickstart', slug: 'docs/quickstart' },
        {
          label: 'Concepts',
          items: [
            { label: 'The spec', slug: 'docs/concepts/spec' },
            { label: 'Targets and failover', slug: 'docs/concepts/targets-and-failover' },
            { label: 'Sticky routing', slug: 'docs/concepts/sticky-routing' },
            { label: 'Reliability principle', slug: 'docs/concepts/reliability-principle' },
          ],
        },
        {
          label: 'Providers',
          items: [
            { label: 'Kubernetes', slug: 'docs/providers/kubernetes' },
            { label: 'Modal', slug: 'docs/providers/modal' },
            { label: 'RunPod', slug: 'docs/providers/runpod' },
            { label: 'Baseten', slug: 'docs/providers/baseten' },
            { label: 'Replicate', slug: 'docs/providers/replicate' },
          ],
        },
        {
          label: 'Router',
          items: [
            { label: 'Overview', slug: 'docs/router/overview' },
            { label: 'Health and circuits', slug: 'docs/router/health-and-circuits' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'CLI', slug: 'docs/reference/cli' },
            { label: 'Spec schema', slug: 'docs/reference/spec-schema' },
          ],
        },
        {
          label: 'Design',
          items: [{ label: 'Architecture plan', slug: 'docs/design/architecture' }],
        },
      ],
    }),
  ],
});
