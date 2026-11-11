import { h } from "preact";
import htm from "htm";

// Tagged-template JSX replacement. No build step: the template is parsed at run time.
export const html = htm.bind(h);
