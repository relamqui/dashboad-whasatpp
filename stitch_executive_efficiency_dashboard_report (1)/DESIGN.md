---
name: Executive Analytics System
colors:
  surface: '#031427'
  surface-dim: '#031427'
  surface-bright: '#2a3a4f'
  surface-container-lowest: '#000f21'
  surface-container-low: '#0b1c30'
  surface-container: '#102034'
  surface-container-high: '#1b2b3f'
  surface-container-highest: '#26364a'
  on-surface: '#d3e4fe'
  on-surface-variant: '#c6c6cd'
  inverse-surface: '#d3e4fe'
  inverse-on-surface: '#213145'
  outline: '#909097'
  outline-variant: '#45464d'
  surface-tint: '#bec6e0'
  primary: '#bec6e0'
  on-primary: '#283044'
  primary-container: '#0f172a'
  on-primary-container: '#798098'
  inverse-primary: '#565e74'
  secondary: '#b4c5ff'
  on-secondary: '#002a78'
  secondary-container: '#0053db'
  on-secondary-container: '#cdd7ff'
  tertiary: '#4edea3'
  on-tertiary: '#003824'
  tertiary-container: '#001c10'
  on-tertiary-container: '#009365'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#dae2fd'
  primary-fixed-dim: '#bec6e0'
  on-primary-fixed: '#131b2e'
  on-primary-fixed-variant: '#3f465c'
  secondary-fixed: '#dbe1ff'
  secondary-fixed-dim: '#b4c5ff'
  on-secondary-fixed: '#00174b'
  on-secondary-fixed-variant: '#003ea8'
  tertiary-fixed: '#6ffbbe'
  tertiary-fixed-dim: '#4edea3'
  on-tertiary-fixed: '#002113'
  on-tertiary-fixed-variant: '#005236'
  background: '#031427'
  on-background: '#d3e4fe'
  surface-variant: '#26364a'
typography:
  display-xl:
    fontFamily: Inter
    fontSize: 48px
    fontWeight: '800'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '800'
    lineHeight: '1.2'
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '700'
    lineHeight: '1.3'
  title-sm:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '600'
    lineHeight: '1.4'
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '500'
    lineHeight: '1.6'
  body-sm:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '500'
    lineHeight: '1.5'
  label-caps:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '700'
    lineHeight: '1'
    letterSpacing: 0.05em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  unit: 8px
  container-padding: 32px
  gutter: 24px
  card-gap: 24px
  section-margin: 48px
---

## Brand & Style

This design system is engineered for high-stakes decision-making environments. It balances the authority of a traditional executive suite with the agility of modern SaaS platforms. The personality is **Professional, Analytical, Technological, and Premium**. 

The visual direction utilizes a **Refined Glassmorphism** approach. By layering translucent surfaces over deep, structured backgrounds, the UI achieves a sense of depth and dimensionality that feels high-end and modern. This style minimizes cognitive load by emphasizing hierarchy through light and shadow rather than heavy borders, creating an interface that feels both expansive and precise.

## Colors

The palette is anchored by **Deep Navy (#0f172a)**, serving as the primary canvas to establish an executive and serious tone. **Royal Blue (#2563eb)** is used strategically for primary actions, data highlights, and active states, providing a vibrant technological contrast.

Sophisticated grays bridge the gap between the dark base and white content, used primarily for secondary text and subtle iconography. Semantic colors are highly saturated: **Emerald Green** for positive growth and success metrics, and **Amber Orange** for alerts or pending actions. All surface colors in this design system should support backdrop filters to maintain the glass effect.

## Typography

The design system utilizes **Inter** for its exceptional legibility and systematic appearance. The hierarchy is driven by extreme weight contrasts.

- **Headlines:** Use **ExtraBold (800)** to command attention and establish a clear entry point for data sections.
- **Body Text:** Use **Medium (500)** as the default for better readability against dark, translucent backgrounds, ensuring text doesn't "thin out" visually.
- **Labels:** Utilize a Bold, all-caps style for metadata and table headers to distinguish them clearly from interactive data points.

## Layout & Spacing

This design system employs a **Fixed Grid** approach for the central analytical dashboard, shifting to a fluid model for internal card layouts. The rhythm is based on an 8px square grid, ensuring mathematical harmony across all components.

Margins are generous (32px+) to evoke a premium, "breathable" feel that prevents the CRM data from feeling cluttered. Content blocks are separated by significant vertical whitespace to delineate different analytical streams clearly.

## Elevation & Depth

Depth is the primary communicator of hierarchy in this design system. It is achieved through three layers:

1.  **The Base:** The Deep Navy canvas (#0f172a).
2.  **The Glass Layer:** Cards and containers use a semi-transparent fill (`rgba(30, 41, 59, 0.7)`) with a `backdrop-filter: blur(12px)`. A thin, 1px inner border (white at 10% opacity) simulates a glass edge.
3.  **The Shadow:** "Deep but light" shadows are applied to elevated cards. These are high-radius (40px-60px), low-opacity (20%) shadows using a dark indigo tint rather than pure black, creating a natural ambient glow.

## Shapes

The shape language is characterized by **Generous Rounding**. Standard containers use `2xl` (1.5rem) or `3xl` (2rem) corner radii. This softness counteracts the "coldness" of the dark theme and the "hardness" of the analytical data, making the technology feel more approachable and modern. Buttons and small tags should follow this lead with fully rounded (pill-shaped) options for interactive elements.

## Components

- **Statistic Cards:** Features a subtle linear gradient background (top-left to bottom-right). Icons should be minimalist line-art (2px stroke) housed in a soft-glow circular container.
- **Tables:** Rows are separated by clean, low-opacity lines (10% white). The header row uses the `label-caps` typography style. No vertical borders are allowed; depth is used instead of lines to separate the table from the background.
- **Status Badges:** Use a "Capsule" shape with a low-opacity background of the semantic color and a high-opacity text color (e.g., Emerald Green text on 15% Emerald Green background).
- **Buttons:** Primary buttons use the Royal Blue with a subtle inner glow. Hover states should increase the backdrop blur and brightness slightly rather than changing the base color drastically.
- **Inputs:** Darker than the card surface, with a 1px border that illuminates in Royal Blue upon focus.