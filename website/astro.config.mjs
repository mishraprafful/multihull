import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightVersions from 'starlight-versions';

export default defineConfig({
  site: process.env.SITE_URL ?? 'https://multihull.pages.dev',
  integrations: [
    starlight({
      title: 'Multihull',
      plugins: [
        starlightVersions({
          current: { label: 'latest' },
          versions: [{ slug: '0.2.0' }, { slug: '0.1.0' }],
        }),
      ],
      description: 'Deploy hot GPU containers to many providers. One URL. Automatic failover.',
      logo: { src: './src/assets/mark.svg', alt: 'Multihull' },
      favicon: '/favicon.svg',
      customCss: ['@fontsource-variable/jetbrains-mono/wght.css', './src/styles/theme.css'],
      components: {
        Hero: './src/components/Hero.astro',
      },
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/mishraprafful/multihull' },
      ],
      sidebar: [
        { label: 'Quickstart', slug: 'docs/quickstart' },
        { label: 'Demo', slug: 'docs/demo' },
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
            { label: 'Controller stream', slug: 'docs/router/controller-stream' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'CLI', slug: 'docs/reference/cli' },
            { label: 'Spec schema', slug: 'docs/reference/spec-schema' },
            { label: 'Security', slug: 'docs/reference/security' },
            { label: 'Release notes', slug: 'docs/reference/release-notes' },
            { label: 'Release notes 0.1.0', slug: 'docs/reference/release-notes/0.1.0' },
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
