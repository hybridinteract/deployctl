# Examples

`demo/` is a deploy directory as it sits in an application repository — only the
committed part, `project/`. `config/` and `generated/` are created on your machine
by `deployctl init` and `deployctl setup` and never committed.

```bash
cd examples/demo && deployctl init --mode single --env staging
```
