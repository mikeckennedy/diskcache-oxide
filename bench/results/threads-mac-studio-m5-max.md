Threads in one process, 1,000 keys of a 1 KB str, median of 5 rounds of 1.0 s, thousands of calls a second. mac-studio-m5-max (18 CPUs), ac power. Arms: 3.14: Python 3.14.10, oxide 0.5.8; 3.14t: Python 3.14.10t, oxide 0.5.8; 3.15t: Python 3.15.2t, oxide 0.5.8.

**reads**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 721k | 696k | 750k | 752k |
| 3.14t, oxide Cache, shared | 695k | 993k | 1,216k | 785k |
| 3.15t, oxide Cache, shared | 706k | 937k | 1,129k | 790k |
| 3.14, oxide Cache, handle per thread | 671k | 675k | 723k | 767k |
| 3.14t, oxide Cache, handle per thread | 791k | 1,022k | 1,167k | 804k |
| 3.15t, oxide Cache, handle per thread | 751k | 1,013k | 1,290k | 798k |
| 3.14, oxide FanoutCache(8), shared | 824k | 812k | 797k | 798k |
| 3.14t, oxide FanoutCache(8), shared | 772k | 903k | 1,307k | 1,017k |
| 3.15t, oxide FanoutCache(8), shared | 766k | 882k | 1,421k | 1,011k |
| 3.14, oxide FanoutCache(8), handle per thread | 815k | 833k | 828k | 824k |
| 3.14t, oxide FanoutCache(8), handle per thread | 771k | 931k | 1,343k | 1,023k |
| 3.15t, oxide FanoutCache(8), handle per thread | 773k | 911k | 1,276k | 999k |
| 3.14, oxide Cache, directory per thread | 756k | 736k | 722k | 747k |
| 3.14t, oxide Cache, directory per thread | 759k | 1,046k | 1,501k | 587k |
| 3.15t, oxide Cache, directory per thread | 718k | 1,022k | 1,454k | 608k |
| 3.14, oxide getter, shared Cache | 40,345k | 40,676k | 40,102k | 40,582k |
| 3.14t, oxide getter, shared Cache | 33,642k | 9,583k | 6,273k | 5,362k |
| 3.15t, oxide getter, shared Cache | 31,091k | 8,582k | 6,077k | 5,157k |
| 3.14, dict lookup | 59,399k | 58,595k | 58,283k | 58,130k |
| 3.14t, dict lookup | 41,439k | 45,106k | 71,614k | 66,315k |
| 3.15t, dict lookup | 39,283k | 43,579k | 68,245k | 63,281k |
| 3.14, diskcache Cache, shared | 449k | 215k | 71k | 18k |
| 3.14t, diskcache Cache, shared | 475k | 500k | 608k | 669k |
| 3.15t, diskcache Cache, shared | 506k | 475k | 703k | 737k |
| 3.14, diskcache FanoutCache(8), shared | 395k | 145k | 64k | 17k |
| 3.14t, diskcache FanoutCache(8), shared | 411k | 588k | 845k | 743k |
| 3.15t, diskcache FanoutCache(8), shared | 428k | 633k | 954k | 801k |

**reads**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 95% | 95% | 104% | 101% |
| 3.14, oxide Cache, handle per thread | 89% | 92% | 100% | 103% |
| 3.14, oxide FanoutCache(8), shared | 109% | 110% | 110% | 107% |
| 3.14, oxide FanoutCache(8), handle per thread | 108% | 113% | 115% | 110% |
| 3.14, oxide getter, shared Cache | 5335% | 5528% | 5554% | 5430% |
| 3.14, dict lookup | 7855% | 7964% | 8073% | 7778% |
| 3.14, diskcache Cache, shared | 59% | 29% | 10% | 2% |
| 3.14, diskcache FanoutCache(8), shared | 52% | 20% | 9% | 2% |
| 3.14t, oxide Cache, shared | 92% | 95% | 81% | 134% |
| 3.14t, oxide Cache, handle per thread | 104% | 98% | 78% | 137% |
| 3.14t, oxide FanoutCache(8), shared | 102% | 86% | 87% | 173% |
| 3.14t, oxide FanoutCache(8), handle per thread | 102% | 89% | 89% | 174% |
| 3.14t, oxide getter, shared Cache | 4432% | 916% | 418% | 914% |
| 3.14t, dict lookup | 5459% | 4312% | 4771% | 11302% |
| 3.14t, diskcache Cache, shared | 63% | 48% | 41% | 114% |
| 3.14t, diskcache FanoutCache(8), shared | 54% | 56% | 56% | 127% |
| 3.15t, oxide Cache, shared | 98% | 92% | 78% | 130% |
| 3.15t, oxide Cache, handle per thread | 105% | 99% | 89% | 131% |
| 3.15t, oxide FanoutCache(8), shared | 107% | 86% | 98% | 166% |
| 3.15t, oxide FanoutCache(8), handle per thread | 108% | 89% | 88% | 164% |
| 3.15t, oxide getter, shared Cache | 4330% | 840% | 418% | 848% |
| 3.15t, dict lookup | 5471% | 4263% | 4694% | 10401% |
| 3.15t, diskcache Cache, shared | 70% | 46% | 48% | 121% |
| 3.15t, diskcache FanoutCache(8), shared | 60% | 62% | 66% | 132% |

**1000:1**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 710k | 725k | 722k | 730k |
| 3.14t, oxide Cache, shared | 699k | 864k | 912k | 722k |
| 3.15t, oxide Cache, shared | 705k | 944k | 1,089k | 735k |
| 3.14, oxide Cache, handle per thread | 719k | 716k | 732k | 722k |
| 3.14t, oxide Cache, handle per thread | 708k | 952k | 1,154k | 736k |
| 3.15t, oxide Cache, handle per thread | 731k | 983k | 1,208k | 731k |
| 3.14, oxide FanoutCache(8), shared | 725k | 787k | 728k | 672k |
| 3.14t, oxide FanoutCache(8), shared | 824k | 939k | 1,300k | 995k |
| 3.15t, oxide FanoutCache(8), shared | 785k | 906k | 1,370k | 1,011k |
| 3.14, oxide FanoutCache(8), handle per thread | 805k | 788k | 801k | 786k |
| 3.14t, oxide FanoutCache(8), handle per thread | 724k | 857k | 1,373k | 989k |
| 3.15t, oxide FanoutCache(8), handle per thread | 806k | 900k | 1,380k | 998k |
| 3.14, oxide Cache, directory per thread | 748k | 740k | 687k | 703k |
| 3.14t, oxide Cache, directory per thread | 729k | 993k | 1,454k | 593k |
| 3.15t, oxide Cache, directory per thread | 732k | 1,048k | 1,463k | 601k |
| 3.14, diskcache Cache, shared | 419k | 191k | 67k | 18k |
| 3.14t, diskcache Cache, shared | 463k | 394k | 506k | 321k |
| 3.15t, diskcache Cache, shared | 478k | 407k | 553k | 310k |
| 3.14, diskcache FanoutCache(8), shared | 399k | 147k | 61k | 18k |
| 3.14t, diskcache FanoutCache(8), shared | 412k | 565k | 759k | 715k |
| 3.15t, diskcache FanoutCache(8), shared | 425k | 612k | 856k | 754k |

**1000:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 95% | 98% | 105% | 104% |
| 3.14, oxide Cache, handle per thread | 96% | 97% | 107% | 103% |
| 3.14, oxide FanoutCache(8), shared | 97% | 106% | 106% | 96% |
| 3.14, oxide FanoutCache(8), handle per thread | 108% | 106% | 117% | 112% |
| 3.14, diskcache Cache, shared | 56% | 26% | 10% | 3% |
| 3.14, diskcache FanoutCache(8), shared | 53% | 20% | 9% | 3% |
| 3.14t, oxide Cache, shared | 96% | 87% | 63% | 122% |
| 3.14t, oxide Cache, handle per thread | 97% | 96% | 79% | 124% |
| 3.14t, oxide FanoutCache(8), shared | 113% | 95% | 89% | 168% |
| 3.14t, oxide FanoutCache(8), handle per thread | 99% | 86% | 94% | 167% |
| 3.14t, diskcache Cache, shared | 64% | 40% | 35% | 54% |
| 3.14t, diskcache FanoutCache(8), shared | 57% | 57% | 52% | 121% |
| 3.15t, oxide Cache, shared | 96% | 90% | 74% | 122% |
| 3.15t, oxide Cache, handle per thread | 100% | 94% | 83% | 122% |
| 3.15t, oxide FanoutCache(8), shared | 107% | 86% | 94% | 168% |
| 3.15t, oxide FanoutCache(8), handle per thread | 110% | 86% | 94% | 166% |
| 3.15t, diskcache Cache, shared | 65% | 39% | 38% | 52% |
| 3.15t, diskcache FanoutCache(8), shared | 58% | 58% | 59% | 126% |

**99:1**, calls a second:

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 654k | 630k | 641k | 579k |
| 3.14t, oxide Cache, shared | 629k | 716k | 865k | 595k |
| 3.15t, oxide Cache, shared | 654k | 683k | 720k | 595k |
| 3.14, oxide Cache, handle per thread | 671k | 650k | 633k | 659k |
| 3.14t, oxide Cache, handle per thread | 630k | 710k | 814k | 612k |
| 3.15t, oxide Cache, handle per thread | 629k | 712k | 879k | 625k |
| 3.14, oxide FanoutCache(8), shared | 656k | 689k | 698k | 658k |
| 3.14t, oxide FanoutCache(8), shared | 549k | 668k | 893k | 815k |
| 3.15t, oxide FanoutCache(8), shared | 667k | 745k | 1,128k | 857k |
| 3.14, oxide FanoutCache(8), handle per thread | 693k | 641k | 665k | 650k |
| 3.14t, oxide FanoutCache(8), handle per thread | 680k | 707k | 922k | 830k |
| 3.15t, oxide FanoutCache(8), handle per thread | 693k | 770k | 977k | 849k |
| 3.14, oxide Cache, directory per thread | 629k | 611k | 607k | 599k |
| 3.14t, oxide Cache, directory per thread | 654k | 933k | 1,355k | 571k |
| 3.15t, oxide Cache, directory per thread | 628k | 962k | 1,380k | 577k |
| 3.14, diskcache Cache, shared | 340k | 211k | 75k | 17k |
| 3.14t, diskcache Cache, shared | 409k | 376k | 422k | 418k |
| 3.15t, diskcache Cache, shared | 428k | 390k | 440k | 431k |
| 3.14, diskcache FanoutCache(8), shared | 352k | 134k | 57k | 15k |
| 3.14t, diskcache FanoutCache(8), shared | 358k | 399k | 420k | 305k |
| 3.15t, diskcache FanoutCache(8), shared | 393k | 457k | 458k | 301k |

**99:1**, as a share of "oxide Cache, directory per thread":

| Arm, variant | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| 3.14, oxide Cache, shared | 104% | 103% | 106% | 97% |
| 3.14, oxide Cache, handle per thread | 107% | 106% | 104% | 110% |
| 3.14, oxide FanoutCache(8), shared | 104% | 113% | 115% | 110% |
| 3.14, oxide FanoutCache(8), handle per thread | 110% | 105% | 110% | 108% |
| 3.14, diskcache Cache, shared | 54% | 35% | 12% | 3% |
| 3.14, diskcache FanoutCache(8), shared | 56% | 22% | 9% | 3% |
| 3.14t, oxide Cache, shared | 96% | 77% | 64% | 104% |
| 3.14t, oxide Cache, handle per thread | 96% | 76% | 60% | 107% |
| 3.14t, oxide FanoutCache(8), shared | 84% | 72% | 66% | 143% |
| 3.14t, oxide FanoutCache(8), handle per thread | 104% | 76% | 68% | 145% |
| 3.14t, diskcache Cache, shared | 62% | 40% | 31% | 73% |
| 3.14t, diskcache FanoutCache(8), shared | 55% | 43% | 31% | 53% |
| 3.15t, oxide Cache, shared | 104% | 71% | 52% | 103% |
| 3.15t, oxide Cache, handle per thread | 100% | 74% | 64% | 108% |
| 3.15t, oxide FanoutCache(8), shared | 106% | 77% | 82% | 149% |
| 3.15t, oxide FanoutCache(8), handle per thread | 110% | 80% | 71% | 147% |
| 3.15t, diskcache Cache, shared | 68% | 41% | 32% | 75% |
| 3.15t, diskcache FanoutCache(8), shared | 63% | 48% | 33% | 52% |
