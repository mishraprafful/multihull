import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: process.env.SITE_URL ?? 'https://multihull.pages.dev',
  integrations: [
    starlight({
      title: 'Multihull',
      description: 'Deploy hot GPU containers to many providers. One URL. Automatic failover.',
      logo: { src: './src/assets/mark.svg', alt: 'Multihull' },
      favicon: '/favicon.svg',
      customCss: ['@fontsource-variable/jetbrains-mono/wght.css', './src/styles/theme.css'],
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/mishraprafful/multihull' },
      ],
      head: [
        {
          tag: 'script',
          content:
            "try{if(!localStorage.getItem('starlight-theme'))localStorage.setItem('starlight-theme','dark')}catch(e){}",
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
            { label: 'Security', slug: 'docs/reference/security' },
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
